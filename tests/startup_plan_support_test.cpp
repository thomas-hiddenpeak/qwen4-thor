// Pure capability selection: no metadata I/O or CUDA implementation is linked
// in the standalone host contract. BuildStartupPlan validates real metadata
// separately; these tests do not treat support as a usable allocation budget.
#include "q4t/model/gemv.h"
#include "q4t/model/model.h"
#include "q4t/runtime/startup_plan.h"
#include "q4t/server/server_options.h"
#include "q4t/test.h"

#include <algorithm>
#include <array>
#include <cstdlib>
#include <optional>
#include <string>

namespace {
constexpr std::array<const char*, 6> kFp8Variables = {
    "Q4T_FP8_PROJ", "Q4T_FP8_ATTN", "Q4T_FP8_GDN", "Q4T_FP8_LMHEAD",
    "Q4T_FP8_SHARED", "Q4T_FP8_HC"};

// Each test owns its environment. Runtime cached selections are tested by the
// standalone stub independently of the raw environment selection.
class CleanFp8Environment {
 public:
  CleanFp8Environment() {
    for (size_t i = 0; i < kFp8Variables.size(); ++i) {
      if (const char* value = std::getenv(kFp8Variables[i])) old_[i] = value;
      if (::unsetenv(kFp8Variables[i]) != 0) std::abort();
    }
  }
  ~CleanFp8Environment() {
    for (size_t i = 0; i < kFp8Variables.size(); ++i) {
      const int rc = old_[i] ? ::setenv(kFp8Variables[i], old_[i]->c_str(), 1)
                            : ::unsetenv(kFp8Variables[i]);
      if (rc != 0) std::abort();
    }
  }

 private:
  std::array<std::optional<std::string>, kFp8Variables.size()> old_;
};

bool Supported(const q4t::model::ModelConfig& cfg,
               const q4t::server::ServerOptions& opts) {
  return q4t::runtime::CheckStartupPlanSupport(cfg, opts).ok();
}

#ifdef Q4T_STARTUP_SUPPORT_HOST_ONLY
std::array<bool, 5> cached_fp8{};
#endif
}  // namespace

#ifdef Q4T_STARTUP_SUPPORT_HOST_ONLY
namespace q4t::model {
bool Fp8ProjEnabled(Fp8Part part) {
  return cached_fp8[static_cast<size_t>(part)];
}
}  // namespace q4t::model
#endif

Q4T_TEST(startup_support_defaults_do_not_require_model_files) {
  CleanFp8Environment clean;
  q4t::model::ModelConfig cfg;
  q4t::server::ServerOptions opts;
  cfg.model_dir = "/does-not-exist/startup-support";
  cfg.index_path = cfg.model_dir + "/missing-index.json";
  cfg.moe_hot_list = cfg.model_dir + "/missing-hot-list.json";
  Q4T_CHECK(Supported(cfg, opts));
  Q4T_CHECK(cfg.moe_resident_slots == 0 && opts.mtp_k == 3);
  return true;
}

Q4T_TEST(startup_support_uses_effective_budget_capacity) {
  CleanFp8Environment clean;
  q4t::model::ModelConfig cfg;
  q4t::server::ServerOptions opts;
  opts.max_len = 999999;
  opts.max_seq = 999;
  opts.max_prefill = 0;
  cfg.max_len = 262144;
  cfg.max_seq = 1;
  cfg.max_prefill = 2048;
  Q4T_CHECK(Supported(cfg, opts));
  cfg.max_len = 262145;
  Q4T_CHECK(!Supported(cfg, opts));
  return true;
}

Q4T_TEST(startup_support_mtp_requires_single_sequence) {
  CleanFp8Environment clean;
  q4t::model::ModelConfig cfg;
  q4t::server::ServerOptions opts;
  cfg.max_seq = 2;
  Q4T_CHECK(!Supported(cfg, opts));
  opts.no_mtp = true;
  Q4T_CHECK(Supported(cfg, opts));
  return true;
}

Q4T_TEST(startup_support_mtp_scratch_and_length_boundaries) {
  CleanFp8Environment clean;
  q4t::model::ModelConfig cfg;
  q4t::server::ServerOptions opts;
  for (int k : {1, 3, 16}) {
    opts.mtp_k = k;
    cfg.max_len = k + 1;
    cfg.max_prefill = std::max(4, k + 1);
    Q4T_CHECK(Supported(cfg, opts));
    --cfg.max_len;
    Q4T_CHECK(!Supported(cfg, opts));
    ++cfg.max_len;
    --cfg.max_prefill;
    Q4T_CHECK(!Supported(cfg, opts));
  }
  cfg.max_len = 8192;
  cfg.max_prefill = 2048;
  for (int k : {0, 17}) {
    opts.mtp_k = k;
    Q4T_CHECK(!Supported(cfg, opts));
  }
  return true;
}

Q4T_TEST(startup_support_plain_capacity_edges) {
  CleanFp8Environment clean;
  q4t::model::ModelConfig cfg;
  q4t::server::ServerOptions opts;
  opts.no_mtp = true;
  cfg.max_seq = 64;
  cfg.max_prefill = 64;
  cfg.max_len = 1;
  Q4T_CHECK(Supported(cfg, opts));
  cfg.max_seq = 65;
  Q4T_CHECK(!Supported(cfg, opts));
  cfg.max_seq = 64;
  cfg.max_prefill = 63;
  Q4T_CHECK(!Supported(cfg, opts));
  cfg.max_prefill = 8192;
  cfg.max_len = 262144;
  Q4T_CHECK(Supported(cfg, opts));
  cfg.max_prefill = 8193;
  Q4T_CHECK(!Supported(cfg, opts));
  return true;
}

Q4T_TEST(startup_support_rejects_zero_and_negative_capacity) {
  CleanFp8Environment clean;
  q4t::server::ServerOptions opts;
  opts.no_mtp = true;
  for (int q4t::model::ModelConfig::*field :
       {&q4t::model::ModelConfig::max_len,
        &q4t::model::ModelConfig::max_seq,
        &q4t::model::ModelConfig::max_prefill}) {
    for (int value : {-1, 0}) {
      q4t::model::ModelConfig cfg;
      cfg.*field = value;
      Q4T_CHECK(!Supported(cfg, opts));
    }
  }
  return true;
}

Q4T_TEST(startup_support_full_resident_and_slot_modes_are_eligible) {
  CleanFp8Environment clean;
  q4t::model::ModelConfig cfg;
  q4t::server::ServerOptions opts;
  for (int slots : {0, 128, 256, 512}) {
    cfg.moe_resident_slots = slots;
    Q4T_CHECK(Supported(cfg, opts));
  }
  return true;
}

Q4T_TEST(startup_support_excludes_media_and_router_capture) {
  CleanFp8Environment clean;
  q4t::model::ModelConfig cfg;
  q4t::server::ServerOptions opts;
  opts.allow_media = true;
  Q4T_CHECK(!Supported(cfg, opts));
  opts.allow_media = false;
  opts.moe_trace_dir = "/does-not-exist/router";
  opts.moe_trace_workload = "support-contract";
  Q4T_CHECK(!Supported(cfg, opts));
  opts.no_mtp = true;
  Q4T_CHECK(!Supported(cfg, opts));
  return true;
}

Q4T_TEST(startup_support_checks_all_fp8_environment_switches) {
  CleanFp8Environment clean;
  q4t::model::ModelConfig cfg;
  q4t::server::ServerOptions opts;
  for (const char* name : kFp8Variables) {
    for (const char* value : {"1", "false", "00", " 0"}) {
      Q4T_CHECK(::setenv(name, value, 1) == 0);
      Q4T_CHECK(!Supported(cfg, opts));
    }
    for (const char* value : {"0", ""}) {
      Q4T_CHECK(::setenv(name, value, 1) == 0);
      Q4T_CHECK(Supported(cfg, opts));
    }
    Q4T_CHECK(::unsetenv(name) == 0);
  }
  return true;
}

Q4T_TEST(startup_support_rejects_shape_changes_before_metadata) {
  CleanFp8Environment clean;
  q4t::server::ServerOptions opts;
  for (int q4t::model::ModelConfig::*field :
       {&q4t::model::ModelConfig::num_layers, &q4t::model::ModelConfig::hs,
        &q4t::model::ModelConfig::hc, &q4t::model::ModelConfig::lowrank,
        &q4t::model::ModelConfig::E, &q4t::model::ModelConfig::moe_is,
        &q4t::model::ModelConfig::shared_is, &q4t::model::ModelConfig::topk,
        &q4t::model::ModelConfig::vocab,
        &q4t::model::ModelConfig::ple_ngram_size,
        &q4t::model::ModelConfig::ple_heads_per_ngram,
        &q4t::model::ModelConfig::ple_capacity_tokens}) {
    q4t::model::ModelConfig cfg;
    ++(cfg.*field);
    Q4T_CHECK(!Supported(cfg, opts));
  }
  return true;
}

#ifdef Q4T_STARTUP_SUPPORT_HOST_ONLY
Q4T_TEST(startup_support_rejects_cached_fp8_after_environment_is_cleared) {
  CleanFp8Environment clean;
  q4t::model::ModelConfig cfg;
  q4t::server::ServerOptions opts;
  for (size_t i = 0; i < cached_fp8.size(); ++i) {
    cached_fp8[i] = true;
    const bool supported = Supported(cfg, opts);
    cached_fp8[i] = false;
    Q4T_CHECK(!supported);
  }
  return true;
}

int main() { return q4t::test::RunAllTests(); }
#endif
