#include "q4t/server/memory_admission.h"

#include <atomic>
#include <limits>
#include <thread>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/server/chat_plan.h"
#include "q4t/server/server_options.h"
#include "q4t/test.h"

namespace {
using q4t::runtime::ParseSystemMemory;
using q4t::runtime::SystemMemory;
using q4t::server::MemoryAdmission;
using q4t::server::MemoryState;
SystemMemory observed{true, 10000, 1000};
MemoryAdmission::Clock::time_point current{};
std::atomic<int> reads{0};
SystemMemory Read() {
  ++reads;
  return observed;
}
MemoryAdmission::Clock::time_point Now() { return current; }
void Configure(MemoryAdmission& gate, uint64_t floor = 100,
               uint64_t margin = 50) {
  observed = {true, 10000, 1000};
  current = {};
  reads = 0;
  gate.Configure(floor, margin, Read, Now);
}
}  // namespace

Q4T_TEST(memory_observation_exact_units_and_zero) {
  auto memory = ParseSystemMemory(
      "Unknown: 123456 MB\nMemTotal:\t 100 kB \t\nMemAvailable: 0 kB\n");
  Q4T_CHECK(memory.valid && memory.total_bytes == 102400 &&
            memory.available_bytes == 0 && !memory.CanAllocate(1));
  memory = ParseSystemMemory("MemAvailable: 99 kB\nMemTotal: 100 kB\n");
  Q4T_CHECK(memory.valid && memory.available_bytes == 101376);
  Q4T_CHECK(memory.CanAllocate(101376) && !memory.CanAllocate(101377));
  return true;
}

Q4T_TEST(memory_observation_rejects_missing_duplicate_overflow_truncated) {
  const std::string total = "MemTotal: 100 kB\n";
  for (const std::string value : {
           "", "-1 kB", "+1 kB", "101 kB", "1 MB", "1kB", "1 kBx",
           "1.0 kB", "18446744073709551615 kB", "18446744073709551616 kB"}) {
    const auto memory = ParseSystemMemory(total + "MemAvailable: " + value + '\n');
    Q4T_CHECK(!memory.valid && memory.available_bytes == 0 &&
              memory.total_bytes == 0);
  }
  for (const std::string value : {
           "", "MemAvailable: 1 kB\n", "MemTotal: 100 kB\n",
           "MemTotal: 0 kB\nMemAvailable: 0 kB\n",
           "MemTotal: 100 kB\nMemAvailable: 1 kB",
           "MemTotal: 100 kB\nMemTotal: 100 kB\nMemAvailable: 1 kB\n",
           "MemTotal: 100 kB\nMemAvailable: 1 kB\nMemAvailable: 1 kB\n"})
    Q4T_CHECK(!ParseSystemMemory(value).valid);
  Q4T_CHECK(!ParseSystemMemory(total + "MemAvailable: 1 kB\n" +
                              std::string(16384, 'x') + '\n').valid);
  return true;
}

Q4T_TEST(memory_observation_live_proc_and_mtp_unknown_declines) {
  const auto actual = q4t::runtime::ReadSystemMemory();
  Q4T_CHECK(actual.valid && actual.total_bytes > 0 &&
            actual.available_bytes <= actual.total_bytes);
  const SystemMemory unknown;
  Q4T_CHECK(!unknown.CanAllocate(0) && !unknown.CanAllocate(1));
  const SystemMemory known{true, 100, 50};
  Q4T_CHECK(known.CanAllocate(50) && !known.CanAllocate(51));
  return true;
}

Q4T_TEST(memory_admission_hysteresis_exact_boundaries) {
  MemoryAdmission gate;
  Configure(gate);
  observed.available_bytes = 149;
  Q4T_CHECK(gate.Observe().state == MemoryState::kLow);
  observed.available_bytes = 150;
  Q4T_CHECK(gate.Observe(true).Allowed());
  observed.available_bytes = 100;
  Q4T_CHECK(gate.Observe(true).Allowed());
  observed.available_bytes = 99;
  Q4T_CHECK(gate.Observe(true).state == MemoryState::kLow);
  observed.available_bytes = 149;
  Q4T_CHECK(!gate.Observe(true).Allowed());
  observed.available_bytes = 150;
  const auto memory = gate.Observe(true);
  Q4T_CHECK(memory.Allowed() && memory.transitions == 4 &&
            memory.floor_bytes == 100 && memory.recovery_bytes == 150);
  return true;
}

Q4T_TEST(memory_admission_invalid_observation_closes_and_requires_recovery) {
  MemoryAdmission gate;
  Configure(gate);
  Q4T_CHECK(gate.Observe().Allowed());
  observed = {false, 10000, 10000};  // Invalid fields cannot stay observable.
  auto memory = gate.Observe(true);
  Q4T_CHECK(memory.state == MemoryState::kUnavailable &&
            memory.failures == 1 && memory.memory.available_bytes == 0);
  Q4T_CHECK(gate.Observe().failures == 1 && reads == 2);
  observed = {true, 10000, 120};
  Q4T_CHECK(gate.Observe(true).state == MemoryState::kLow);
  observed.available_bytes = 150;
  memory = gate.Observe(true);
  Q4T_CHECK(memory.Allowed() && memory.failures == 1 &&
            memory.observations == 4);
  return true;
}

Q4T_TEST(memory_admission_sample_age_and_forced_handoff) {
  MemoryAdmission gate;
  Configure(gate);
  Q4T_CHECK(gate.Observe().Allowed());
  observed = {};
  current += std::chrono::milliseconds(249);
  Q4T_CHECK(gate.Observe().Allowed() && reads == 1);
  current += std::chrono::milliseconds(1);
  Q4T_CHECK(!gate.Observe().Allowed() && reads == 2);
  observed = {true, 10000, 1000};
  Q4T_CHECK(!gate.Observe().Allowed());
  Q4T_CHECK(gate.Observe(true).Allowed() && reads == 3);
  current -= std::chrono::seconds(1);
  Q4T_CHECK(gate.Observe().Allowed() && reads == 4);
  return true;
}

Q4T_TEST(memory_admission_disabled_never_reads_and_reconfigure_resets) {
  MemoryAdmission gate;
  Configure(gate, 0);
  observed = {};
  auto memory = gate.Observe(true);
  Q4T_CHECK(memory.Allowed() && memory.state == MemoryState::kDisabled &&
            reads == 0 && memory.recovery_bytes == 0);
  gate.Configure(100, 50, Read, Now);
  Q4T_CHECK(!gate.Observe().Allowed() && reads == 1);
  gate.Configure(0, 50, Read, Now);
  memory = gate.Observe();
  Q4T_CHECK(memory.Allowed() && memory.observations == 0);
  return true;
}

Q4T_TEST(memory_admission_concurrent_checks_share_coherent_sample) {
  MemoryAdmission gate;
  Configure(gate);
  std::atomic<int> allowed{0};
  std::vector<std::thread> threads;
  for (int i = 0; i < 16; ++i)
    threads.emplace_back([&] {
      for (int j = 0; j < 100; ++j) {
        const auto memory = gate.Observe();
        if (memory.Allowed() && memory.memory.available_bytes == 1000 &&
            memory.observations == 1)
          ++allowed;
      }
    });
  for (auto& thread : threads) thread.join();
  Q4T_CHECK(allowed == 1600 && reads == 1);
  return true;
}

Q4T_TEST(memory_admission_cli_defaults_disable_and_transactional_bounds) {
  q4t::server::ServerOptions options;
  options.model_dir = "/read-only-model";
  Q4T_CHECK(options.min_available_memory_mib == 2048 &&
            options.memory_recovery_margin_mib == 512);
  const auto parse = [&](std::initializer_list<std::string_view> args) {
    return ParseServerOptions({args.begin(), args.size()}, &options).ok();
  };
  Q4T_CHECK(parse({"--min-available-memory-mib", "0", "--no-budget"}));
  Q4T_CHECK(options.no_budget && options.min_available_memory_mib == 0);
  for (const auto value : {"-1", "1048577", "1.5", "1x", "2147483648"}) {
    Q4T_CHECK(!parse({"--min-available-memory-mib", value}));
    Q4T_CHECK(options.min_available_memory_mib == 0);
  }
  for (const auto value : {"-1", "0", "1048577", "1.5", "2147483648"}) {
    Q4T_CHECK(!parse({"--memory-recovery-margin-mib", value}));
    Q4T_CHECK(options.memory_recovery_margin_mib == 512);
  }
  Q4T_CHECK(parse({"--min-available-memory-mib", "1048576",
                   "--memory-recovery-margin-mib", "1048576"}));
  return true;
}

Q4T_TEST(memory_admission_configuration_and_capability_boundary) {
  q4t::server::ServerOptions options;
  q4t::server::ChatPlanConfig config;
  q4t::io::Json parsed;
  const auto load = [&] {
    return q4t::io::ParseJson(
        EffectiveServeConfigJson(options, config, false, true, 16), &parsed);
  };
  Q4T_CHECK(load());
  const auto* memory = parsed.Find("memory_admission");
  Q4T_CHECK(memory && memory->GetBool("enabled") &&
            memory->GetInt("recovery_threshold_mib") == 2560 &&
            !memory->GetBool("hard_limit", true));
  Q4T_CHECK(memory->Find("min_available_mib")->GetString("source") ==
            "builtin_default");
  options.min_available_memory_mib = 0;
  options.supplied_options = {"--min-available-memory-mib"};
  Q4T_CHECK(load());
  memory = parsed.Find("memory_admission");
  Q4T_CHECK(!memory->GetBool("enabled", true) &&
            memory->GetInt("recovery_threshold_mib", -1) == 0 &&
            memory->Find("recovery_margin_mib")->GetInt("effective", -1) == 0 &&
            memory->Find("min_available_mib")->GetString("source") == "cli");
  Q4T_CHECK(q4t::io::ParseJson(
      q4t::server::ChatCapabilitiesJson("model", false, true), &parsed));
  memory = parsed.Find("memory_admission");
  Q4T_CHECK(memory && memory->GetBool("liveness_unaffected") &&
            !memory->GetBool("hard_limit", true) &&
            memory->GetString("active_generation") == "continue");
  return true;
}
