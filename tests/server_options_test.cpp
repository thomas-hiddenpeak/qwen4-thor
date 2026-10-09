#include "q4t/server/server_options.h"
#include "q4t/test.h"

#include <initializer_list>
#include <limits>
#include <string_view>

namespace {
using q4t::server::CapabilitiesFor;
using q4t::server::EffectiveMtpVerifier;
using q4t::server::ServerOptions;
ServerOptions Defaults() {
  ServerOptions options;
  options.model_dir = "/read-only-model";
  return options;
}
bool Parse(std::initializer_list<std::string_view> args, ServerOptions* out) {
  return q4t::server::ParseServerOptions({args.begin(), args.size()}, out).ok();
}
}  // namespace

Q4T_TEST(serve_default_is_single_text_greedy) {
  auto options = Defaults();
  Q4T_CHECK(Parse({}, &options));
  const auto capabilities = CapabilitiesFor(options);
  Q4T_CHECK(options.max_seq == 1 && options.no_mtp && !options.allow_media);
  Q4T_CHECK(!capabilities.Experimental());
  Q4T_CHECK(Parse({"--max-seq", "1", "--max-prefill", "8192", "--max-len",
                   "208896", "--no-mtp"},
                  &options));
  Q4T_CHECK(!CapabilitiesFor(options).Experimental());
  return true;
}

Q4T_TEST(serve_capabilities_allow_explicit_combinations) {
  for (int mask = 0; mask < 8; ++mask) {
    auto options = Defaults();
    options.no_mtp = !(mask & 1);
    options.allow_media = bool(mask & 2);
    options.max_seq = (mask & 4) ? 2 : 1;
    const bool supported = !(mask & 1) || !(mask & 6);
    Q4T_CHECK(q4t::server::ValidateServerOptions(options).ok() == supported);
    if (!supported) continue;
    const auto c = CapabilitiesFor(options);
    Q4T_CHECK(c.mtp == bool(mask & 1));
    Q4T_CHECK(c.media == bool(mask & 2));
    Q4T_CHECK(c.multiple_sequences == bool(mask & 4));
    Q4T_CHECK(c.Experimental() == ((mask & 6) != 0));
  }
  for (const auto args : {
           std::initializer_list<std::string_view>{"--mtp", "--max-seq", "2"},
           {"--mtp", "--allow-media"},
           {"--max-seq", "2", "--allow-media", "--mtp"}}) {
    auto options = Defaults();
    Q4T_CHECK(!Parse(args, &options));
    Q4T_CHECK(options.no_mtp && options.max_seq == 1 && !options.allow_media);
  }
  auto options = Defaults();
  Q4T_CHECK(Parse({"--mtp", "--no-mtp"}, &options));
  Q4T_CHECK(!CapabilitiesFor(options).mtp);
  Q4T_CHECK(Parse({"--no-mtp", "--mtp"}, &options));
  Q4T_CHECK(CapabilitiesFor(options).mtp);
  return true;
}

Q4T_TEST(serve_rejects_malformed_numeric_options) {
  for (const auto flag : {"--port", "--max-seq", "--max-len", "--max-prefill",
                          "--max-tokens", "--mem-fraction"}) {
    for (const auto value : {"", "junk", "1tail", " 1", "1 ", "--mtp",
                             "9999999999999999999999999999999999999999"}) {
      auto options = Defaults();
      Q4T_CHECK(!Parse({flag, value}, &options));
      Q4T_CHECK(options.max_seq == 1 && options.no_mtp);
    }
    auto options = Defaults();
    Q4T_CHECK(!Parse({flag}, &options));
  }
  for (const auto value : {"0", "-1", "1.5", "2147483647"}) {
    auto options = Defaults();
    Q4T_CHECK(!Parse({"--max-seq", value}, &options));
  }
  for (const auto value : {"nan", "inf", "-inf", "0", "-0.1", "1.01"}) {
    auto options = Defaults();
    Q4T_CHECK(!Parse({"--mem-fraction", value}, &options));
  }
  return true;
}

Q4T_TEST(serve_validates_before_load_and_preserves_failed_parse) {
  auto options = Defaults();
  Q4T_CHECK(!Parse({"--mtp", "--port", "65536"}, &options));
  Q4T_CHECK(options.no_mtp && options.port == 8000);
  Q4T_CHECK(!Parse({"--host", "not-an-address"}, &options));
  Q4T_CHECK(!Parse({"--model-dir", ""}, &options));
  Q4T_CHECK(!Parse({"--unknown"}, &options));
  Q4T_CHECK(!Parse({"--max-tokens", "0"}, &options));
  Q4T_CHECK(!Parse({"--max-len", "-1"}, &options));
  Q4T_CHECK(!Parse({"--max-prefill", "-1"}, &options));
  Q4T_CHECK(Parse({"--port", "65535", "--max-prefill", "0", "--max-len", "0",
                   "--mem-fraction", "1", "--no-budget"},
                  &options));
  options.max_seq = 0;
  Q4T_CHECK(!q4t::server::ValidateServerOptions(options).ok());
  options = Defaults();
  options.mem_fraction = std::numeric_limits<double>::quiet_NaN();
  Q4T_CHECK(!q4t::server::ValidateServerOptions(options).ok());
  return true;
}

Q4T_TEST(serve_prefill_bound_precedes_workspace_sizing) {
  for (const auto value : {"0", "8192"}) {
    auto options = Defaults();
    Q4T_CHECK(Parse({"--max-prefill", value}, &options));
    Q4T_CHECK(q4t::server::ValidateServerOptions(options).ok());
  }
  for (const auto value : {"8193", "214748365", "2147483647"}) {
    auto options = Defaults();
    options.max_prefill = 8192;
    Q4T_CHECK(!Parse({"--mtp", "--max-prefill", value}, &options));
    Q4T_CHECK(options.max_prefill == 8192 && options.no_mtp);
  }
  // ChatServer::Start also calls validation for programmatic options before
  // loading the tokenizer or invoking any workspace sizing function.
  for (int value : {8193, 214748365, std::numeric_limits<int>::max()}) {
    auto options = Defaults();
    options.max_prefill = value;
    Q4T_CHECK(!q4t::server::ValidateServerOptions(options).ok());
    options.no_budget = true;
    Q4T_CHECK(!q4t::server::ValidateServerOptions(options).ok());
  }
  return true;
}

Q4T_TEST(serve_trace_options_are_explicit_and_bounded) {
  auto options = Defaults();
  Q4T_CHECK(options.moe_trace_dir.empty());
  Q4T_CHECK(!Parse({"--moe-trace-dir", ".q4t-work/trace"}, &options));
  Q4T_CHECK(options.moe_trace_dir.empty());
  Q4T_CHECK(Parse({"--moe-trace-dir", ".q4t-work/trace",
                   "--moe-trace-workload", ".q4t-work/workload.json",
                   "--moe-trace-max-mib", "128"}, &options));
  Q4T_CHECK(options.moe_trace_max_mib == 128);
  for (const auto value : {"0", "4097", "1.5", "-1", "nan"}) {
    Q4T_CHECK(!Parse({"--moe-trace-max-mib", value}, &options));
    Q4T_CHECK(options.moe_trace_max_mib == 128);
  }
  return true;
}

Q4T_TEST(serve_mtp_verifier_defaults_to_sequential) {
  using q4t::server::MtpVerifier;
  auto options = Defaults();
  Q4T_CHECK(!options.mtp_verifier);
  Q4T_CHECK(Parse({"--mtp"}, &options));
  Q4T_CHECK(EffectiveMtpVerifier(options) == MtpVerifier::kSequential);
  Q4T_CHECK(Parse({"--mtp-verifier", "sequential", "--mtp"}, &options));
  Q4T_CHECK(options.mtp_verifier == MtpVerifier::kSequential);
  Q4T_CHECK(!CapabilitiesFor(options).Experimental());
  Q4T_CHECK(Parse({"--max-prefill", "4"}, &options));
  Q4T_CHECK(Parse({"--mtp", "--mtp-verifier", "t4"}, &options));
  Q4T_CHECK(options.mtp_verifier == MtpVerifier::kT4);
  Q4T_CHECK(CapabilitiesFor(options).Experimental());
  return true;
}

Q4T_TEST(serve_mtp_verifier_invalid_parse_is_transactional) {
  for (const auto args : {
           std::initializer_list<std::string_view>{"--mtp-verifier", "t4"},
           {"--mtp-verifier", "sequential"},
           {"--mtp", "--mtp-verifier", "t4", "--no-mtp"},
           {"--mtp", "--mtp-verifier", "sequential", "--no-mtp"},
           {"--mtp", "--mtp-verifier"},
           {"--mtp", "--mtp-verifier", ""},
           {"--mtp", "--mtp-verifier", "--no-mtp"},
           {"--mtp", "--mtp-verifier", "T4"},
           {"--mtp", "--mtp-verifier", "fast"},
           {"--mtp", "--mtp-verifier", "sequential", "--max-prefill", "3"}}) {
    auto options = Defaults();
    Q4T_CHECK(!Parse(args, &options));
    Q4T_CHECK(options.no_mtp && !options.mtp_verifier);
  }
  auto options = Defaults();
  options.mtp_verifier = q4t::server::MtpVerifier::kT4;
  Q4T_CHECK(!q4t::server::ValidateServerOptions(options).ok());
  options.no_mtp = false;
  options.mtp_verifier = static_cast<q4t::server::MtpVerifier>(-1);
  Q4T_CHECK(!q4t::server::ValidateServerOptions(options).ok());
  return true;
}

Q4T_TEST(serve_mtp_default_cannot_bypass_sequential_capacity_checks) {
  for (const auto value : {"1", "2", "3"}) {
    auto options = Defaults();
    Q4T_CHECK(!Parse({"--mtp", "--max-prefill", value}, &options));
    Q4T_CHECK(options.no_mtp && !options.mtp_verifier);
    Q4T_CHECK(!Parse({"--mtp", "--mtp-verifier", "sequential",
                      "--max-prefill", value}, &options));
    // Plain mode and explicitly experimental T4 keep their prior contract.
    Q4T_CHECK(Parse({"--max-prefill", value}, &options));
    Q4T_CHECK(Parse({"--mtp", "--mtp-verifier", "t4"}, &options));
  }
  return true;
}

Q4T_TEST(serve_mtp_reference_configuration_uses_effective_state) {
  using q4t::server::MatchesMtpReferenceConfiguration;
  auto requested = Defaults();
  Q4T_CHECK(Parse({"--mtp", "--max-len", "208896", "--max-prefill", "8192"},
                  &requested));
  Q4T_CHECK(MatchesMtpReferenceConfiguration(requested, true, true, 3, false));
  auto effective = requested;
  effective.max_len = 65536;  // A successful budget may still reduce capacity.
  Q4T_CHECK(!MatchesMtpReferenceConfiguration(effective, true, true, 3, false));
  effective = requested;
  effective.max_prefill = 2048;
  Q4T_CHECK(!MatchesMtpReferenceConfiguration(effective, true, true, 3, false));
  Q4T_CHECK(!MatchesMtpReferenceConfiguration(
      requested, false, true, 3, false));
  Q4T_CHECK(!MatchesMtpReferenceConfiguration(
      requested, true, false, 3, false));
  Q4T_CHECK(!MatchesMtpReferenceConfiguration(requested, true, true, 2, false));
  Q4T_CHECK(!MatchesMtpReferenceConfiguration(requested, true, true, 3, true));
  for (int mutation = 0; mutation < 5; ++mutation) {
    effective = requested;
    if (mutation == 0) effective.mtp_verifier = q4t::server::MtpVerifier::kT4;
    if (mutation == 1) effective.no_mtp = true;
    if (mutation == 2) effective.allow_media = true;
    if (mutation == 3) effective.max_seq = 2;
    if (mutation == 4) effective.no_budget = true;
    Q4T_CHECK(!MatchesMtpReferenceConfiguration(effective, true, true, 3,
                                               false));
  }
  return true;
}

Q4T_TEST(serve_mtp_reference_environment_excludes_arithmetic_overrides) {
  using q4t::server::IsMtpReferenceEnvironmentOverride;
  for (const auto name : {"Q4T_FP8_PROJ", "Q4T_FP8_HC", "Q4T_FP8_ATTN",
                          "Q4T_FP8_GDN", "Q4T_FP8_SHARED", "Q4T_FP8_LMHEAD",
                          "Q4T_FP8_FUTURE", "Q4T_GDN_REG", "Q4T_GDN_CHUNKED",
                          "Q4T_GDN_SPLIT", "Q4T_MOE_STREAMS",
                          "Q4T_MOE_BATCH_GATHER"}) {
    Q4T_CHECK(IsMtpReferenceEnvironmentOverride(name));
  }
  for (const auto name : {"PATH", "CUDA_VISIBLE_DEVICES", "Q4T_LOAD_TIMING",
                          "Q4T_MTP_INIT_TIMING", "Q4T_GDN_DIAGNOSTIC"}) {
    Q4T_CHECK(!IsMtpReferenceEnvironmentOverride(name));
  }
  return true;
}
