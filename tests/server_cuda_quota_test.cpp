#include "q4t/server/cuda_quota_policy.h"

#include <limits>
#include <string_view>

#include "q4t/io/json.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/server/chat_plan.h"
#include "q4t/server/cuda_quota_metrics.h"
#include "q4t/server/device_memory.h"
#include "q4t/test.h"

namespace {
using namespace q4t;
using namespace q4t::server;
using namespace std::literals;
ServerOptions Defaults() {
  ServerOptions options;
  options.model_dir = "/read-only-model";
  return options;
}
}  // namespace

Q4T_TEST(cuda_quota_options_default_explicit_disabled_and_no_budget) {
  for (const auto value : {"auto"sv, "0"sv, "1048576"sv}) {
    auto options = Defaults();
    Q4T_CHECK(ValidateServerOptions(options).ok());
    const std::string_view args[] = {"--no-budget",
                                     "--cuda-allocation-limit-mib", value};
    Q4T_CHECK(ParseServerOptions(args, &options).ok());
    Q4T_CHECK(options.no_budget &&
              options.cuda_allocation_limit_mib == (value == "auto" ? -1
                                                    : value == "0"  ? 0
                                                                    : 1048576));
  }
  Q4T_CHECK(ServerOptions{}.cuda_allocation_limit_mib == -1);
  return true;
}

Q4T_TEST(cuda_quota_options_reject_ambiguous_or_out_of_range) {
  for (const auto value : {"-1"sv, "-0"sv, "1.0"sv, "AUTO"sv, "1048577"sv,
                           "18446744073709551615"sv}) {
    auto options = Defaults();
    Q4T_CHECK(ValidateServerOptions(options).ok());
    const std::string_view args[] = {"--cuda-allocation-limit-mib", value};
    Q4T_CHECK(!ParseServerOptions(args, &options).ok());
  }
  auto options = Defaults();
  Q4T_CHECK(ValidateServerOptions(options).ok());
  options.cuda_allocation_limit_mib = -2;
  Q4T_CHECK(!ValidateServerOptions(options).ok());
  options.cuda_allocation_limit_mib = 1048577;
  Q4T_CHECK(!ValidateServerOptions(options).ok());
  return true;
}

Q4T_TEST(cuda_quota_auto_uses_total_and_requires_valid_observation) {
  uint64_t bytes = 0;
  runtime::SystemMemory memory;
  memory.valid = true;
  memory.total_bytes = 101;
  memory.available_bytes = 1;
  Q4T_CHECK(ResolveCudaQuotaLimit(-1, 0.5, memory, &bytes).ok() && bytes == 50);
  memory.available_bytes = 101;
  Q4T_CHECK(ResolveCudaQuotaLimit(-1, 0.5, memory, &bytes).ok() && bytes == 50);
  memory.available_bytes = 102;
  Q4T_CHECK(!ResolveCudaQuotaLimit(-1, 0.5, memory, &bytes).ok());
  memory.available_bytes = 1;
  for (double fraction :
       {0.0, -1.0, 1.1, std::numeric_limits<double>::infinity(),
        std::numeric_limits<double>::quiet_NaN()})
    Q4T_CHECK(!ResolveCudaQuotaLimit(-1, fraction, memory, &bytes).ok());
  Q4T_CHECK(!ResolveCudaQuotaLimit(-1, 0.9, {}, &bytes).ok());
  Q4T_CHECK(!ResolveCudaQuotaLimit(-1, 0.5, memory, nullptr).ok());
  return true;
}

Q4T_TEST(cuda_quota_explicit_does_not_read_or_depend_on_memory_sample) {
  uint64_t bytes = 42;
  Q4T_CHECK(ResolveCudaQuotaLimit(0, 0, {}, &bytes).ok() && bytes == 0);
  Q4T_CHECK(ResolveCudaQuotaLimit(1048576, 0, {}, &bytes).ok() &&
            bytes == (uint64_t{1} << 40));
  Q4T_CHECK(!ResolveCudaQuotaLimit(1048577, 1, {}, &bytes).ok());
  return true;
}

Q4T_TEST(cuda_quota_config_preserves_source_and_independent_limit) {
  ServerOptions options;
  options.no_budget = true;
  ChatPlanConfig config;
  config.cuda_quota_limit_bytes = uint64_t{90} << 30;
  for (int input : {-1, 0, 102400}) {
    options.cuda_allocation_limit_mib = input;
    config.cuda_quota_limit_bytes = input == 0 ? 0 : uint64_t{90} << 30;
    io::Json root;
    Q4T_CHECK(
        io::ParseJson(
            EffectiveServeConfigJson(options, config, false, false, 8), &root)
            .ok());
    const auto* quota = root.Find("cuda_allocation_quota");
    Q4T_CHECK(quota && quota->GetBool("enabled") == (input != 0));
    Q4T_CHECK(!quota->GetBool("no_budget_bypasses_quota", true) &&
              !quota->GetBool("physical_ram_limit", true) &&
              !quota->GetBool("external_library_calls_covered", true));
    Q4T_CHECK(quota->GetString("source") ==
              (input == -1 ? "builtin_default" : "server_options"));
    Q4T_CHECK(quota->GetInt("effective_limit_bytes") ==
              static_cast<int64_t>(config.cuda_quota_limit_bytes));
  }
  options.supplied_options = {"--cuda-allocation-limit-mib"};
  io::Json root;
  Q4T_CHECK(
      io::ParseJson(EffectiveServeConfigJson(options, config, false, false, 8),
                    &root)
          .ok());
  Q4T_CHECK(root.Find("cuda_allocation_quota")->GetString("source") == "cli");
  return true;
}

Q4T_TEST(cuda_quota_metrics_report_complete_partition_and_failures) {
  runtime::CudaQuotaSnapshot value;
  value.enabled = true;
  value.memory.limit_bytes = 100;
  value.memory.charged_bytes = 15;
  value.memory.reserved_bytes = 1;
  value.memory.live_bytes = 2;
  value.memory.releasing_bytes = 3;
  value.memory.pending_bytes = 4;
  value.memory.unknown_bytes = 5;
  value.memory.peak_bytes = 99;
  value.memory.poisoned = true;
  value.byte_rejections = 6;
  value.entry_rejections = 7;
  value.stream_rejections = 8;
  value.contract_failures = 9;
  value.backend_failures = 10;
  const auto metrics = CudaQuotaMetrics(value);
  for (const auto line :
       {"charged_bytes 15\n", "reserved_bytes 1\n", "live_bytes 2\n",
        "releasing_bytes 3\n", "pending_bytes 4\n", "unknown_bytes 5\n",
        "peak_bytes 99\n", "entry_capacity 16384\n", "stream_capacity 256\n",
        "poisoned 1\n", "byte_rejections_total 6\n",
        "entry_rejections_total 7\n", "stream_rejections_total 8\n",
        "contract_failures_total 9\n", "backend_failures_total 10\n"})
    Q4T_CHECK(metrics.find(std::string("q4t_cuda_quota_") + line) !=
              std::string::npos);
  return true;
}

Q4T_TEST(cuda_quota_reason_does_not_make_unknown_failure_recoverable) {
  DeviceMemoryResult result{DeviceMemoryState::kExhausted, 2, 0, true};
  Q4T_CHECK(result.Reason() == "cuda_allocation_quota_exhausted"sv);
  std::atomic<bool> healthy{true}, failed{false}, stop{false};
  ApplyDeviceMemoryFailure(result, healthy, failed, stop);
  Q4T_CHECK(healthy && !failed && !stop);
  result.state = DeviceMemoryState::kFailure;
  Q4T_CHECK(result.Reason() == "device_memory_failure"sv);
  ApplyDeviceMemoryFailure(result, healthy, failed, stop);
  Q4T_CHECK(!healthy && failed && stop);
  return true;
}
