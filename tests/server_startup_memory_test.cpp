#include "q4t/server/startup_admission.h"

#include <limits>

#include "q4t/io/json.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/server/chat_plan.h"
#include "q4t/server/server_options.h"
#include "q4t/test.h"

namespace {
using q4t::runtime::SystemMemory;
using q4t::server::ObserveStartupMemory;
using q4t::server::StartupMemoryState;
SystemMemory observed{true, 1000, 500};
int reads = 0;
SystemMemory Read() {
  ++reads;
  return observed;
}
}  // namespace

Q4T_TEST(startup_memory_estimate_plus_headroom_exact_boundary) {
  observed = {true, 1000, 499};
  auto value = ObserveStartupMemory(true, 400, 100, true, Read);
  Q4T_CHECK(!value.Allowed() && value.required_bytes == 500 &&
            value.state == StartupMemoryState::kInsufficient);
  observed.available_bytes = 500;
  value = ObserveStartupMemory(true, 400, 100, true, Read);
  Q4T_CHECK(value.Allowed() && value.state == StartupMemoryState::kAdmitted);
  // Total RAM is not a substitute for current availability.
  observed = {true, 1000000, 499};
  Q4T_CHECK(!ObserveStartupMemory(true, 400, 100, true, Read).Allowed());
  return true;
}

Q4T_TEST(startup_memory_unknown_and_inconsistent_samples_fail_closed) {
  for (const auto memory : {SystemMemory{}, SystemMemory{false, 1000, 900},
                           SystemMemory{true, 0, 0},
                           SystemMemory{true, 1000, 1001}}) {
    observed = memory;
    const auto value = ObserveStartupMemory(true, 400, 100, true, Read);
    Q4T_CHECK(!value.Allowed() && !value.memory.valid &&
              value.memory.available_bytes == 0 &&
              value.state == StartupMemoryState::kObservationUnavailable);
  }
  Q4T_CHECK(!ObserveStartupMemory(true, 400, 100, true, nullptr).Allowed());
  return true;
}

Q4T_TEST(startup_memory_zero_available_is_known_rejection) {
  observed = {true, 1000, 0};
  const auto value = ObserveStartupMemory(true, 400, 100, true, Read);
  Q4T_CHECK(!value.Allowed() && value.memory.valid &&
            value.state == StartupMemoryState::kInsufficient);
  q4t::io::Json json;
  Q4T_CHECK(q4t::io::ParseJson(StartupMemorySnapshotJson(value), &json).ok());
  Q4T_CHECK(json.Find("observed_available_bytes")->IsNumber() &&
            json.GetInt("observed_available_bytes", -1) == 0);
  observed = {};
  Q4T_CHECK(q4t::io::ParseJson(StartupMemorySnapshotJson(
      ObserveStartupMemory(true, 400, 100, true, Read)), &json).ok());
  Q4T_CHECK(json.Find("observed_available_bytes")->IsNull());
  return true;
}

Q4T_TEST(startup_memory_missing_estimate_and_overflow_precede_observation) {
  reads = 0;
  auto value = ObserveStartupMemory(true, 0, 100, true, Read);
  Q4T_CHECK(!value.Allowed() &&
            value.state == StartupMemoryState::kEstimateUnavailable);
  value = ObserveStartupMemory(true, std::numeric_limits<uint64_t>::max(),
                               1, true, Read);
  Q4T_CHECK(!value.Allowed() && value.required_bytes == 0 &&
            value.state == StartupMemoryState::kRequiredOverflow && reads == 0);
  return true;
}

Q4T_TEST(startup_memory_disabled_paths_do_not_read_or_claim_admission) {
  reads = 0;
  auto value = ObserveStartupMemory(false, 0, 100, true, Read);
  Q4T_CHECK(value.Allowed() &&
            value.state == StartupMemoryState::kBudgetDisabled);
  value = ObserveStartupMemory(true, 400, 0, true, Read);
  Q4T_CHECK(value.Allowed() && value.state == StartupMemoryState::kDisabled &&
            !value.memory.valid && value.required_bytes == 0 && reads == 0);
  return true;
}

Q4T_TEST(startup_memory_floor_checkpoints_take_fresh_samples) {
  observed = {true, 1000, 100};
  reads = 0;
  auto value = ObserveStartupMemory(true, 0, 100, false, Read);
  Q4T_CHECK(value.Allowed() && value.required_bytes == 100);
  observed.available_bytes = 99;
  value = ObserveStartupMemory(true, 0, 100, false, Read);
  Q4T_CHECK(!value.Allowed() &&
            value.state == StartupMemoryState::kInsufficient && reads == 2);
  return true;
}

Q4T_TEST(startup_memory_options_validate_before_mutating) {
  q4t::server::ServerOptions options;
  options.model_dir = "/read-only-model";
  Q4T_CHECK(options.startup_memory_headroom_mib == 2048);
  const auto parse = [&](std::initializer_list<std::string_view> args) {
    return ParseServerOptions({args.begin(), args.size()}, &options).ok();
  };
  Q4T_CHECK(parse({"--startup-memory-headroom-mib", "0"}));
  for (const auto value : {"-1", "1048577", "1.5", "1x", "2147483648"}) {
    Q4T_CHECK(!parse({"--startup-memory-headroom-mib", value}));
    Q4T_CHECK(options.startup_memory_headroom_mib == 0);
  }
  Q4T_CHECK(parse({"--startup-memory-headroom-mib", "1048576", "--no-budget"}));
  Q4T_CHECK(options.no_budget && options.startup_memory_headroom_mib == 1048576);
  options.startup_memory_headroom_mib = -1;
  Q4T_CHECK(!ValidateServerOptions(options).ok());
  return true;
}

Q4T_TEST(startup_memory_configuration_distinguishes_policy_and_observation) {
  using namespace q4t::server;
  ServerOptions options;
  ChatPlanConfig config;
  q4t::io::Json json;
  Q4T_CHECK(q4t::io::ParseJson(EffectiveServeConfigJson(
      options, config, true, true, 128), &json).ok());
  const auto* policy = json.Find("startup_memory");
  Q4T_CHECK(policy && policy->GetBool("enabled") &&
            policy->Find("checks")->IsNull() &&
            !policy->GetBool("hard_limit", true));
  Q4T_CHECK(policy->Find("headroom_mib")->GetInt("effective") == 2048);
  observed = {true, 1000, 500};
  StartupMemoryChecks checks;
  checks.before_metadata = ObserveStartupMemory(true, 0, 100, false, Read);
  checks.before_allocations = ObserveStartupMemory(true, 400, 100, true, Read);
  checks.before_ready = ObserveStartupMemory(true, 0, 100, false, Read);
  Q4T_CHECK(q4t::io::ParseJson(EffectiveServeConfigJson(
      options, config, true, true, 128, &checks), &json).ok());
  policy = json.Find("startup_memory");
  Q4T_CHECK(policy->Find("checks")->Find("before_allocations")
                ->GetInt("required_bytes") == 500);
  Q4T_CHECK(q4t::io::ParseJson(EffectiveServeConfigJson(
      options, config, false, false, 128), &json).ok());
  policy = json.Find("startup_memory");
  Q4T_CHECK(!policy->GetBool("enabled", true) &&
            policy->Find("headroom_mib")->GetInt("effective", -1) == 0 &&
            policy->Find("headroom_mib")->GetString("resolution") ==
                "budget_disabled");
  Q4T_CHECK(json.Find("memory_admission")->GetBool("enabled"));
  Q4T_CHECK(q4t::io::ParseJson(ChatCapabilitiesJson("model", false, true),
                              &json).ok());
  Q4T_CHECK(json.Find("startup_memory")->GetBool("no_budget_bypasses_check"));
  return true;
}
