#include "q4t/server/server_options.h"

#include <arpa/inet.h>

#include <charconv>
#include <cmath>
#include <limits>

namespace q4t::server {

ServerCapabilities CapabilitiesFor(const ServerOptions& options) {
  return {!options.no_mtp, options.allow_media, options.max_seq > 1,
          EffectiveMtpVerifier(options)};
}

bool IsMtpReferenceEnvironmentOverride(std::string_view name) {
  return name.starts_with("Q4T_FP8") || name == "Q4T_GDN_REG" ||
         name == "Q4T_GDN_CHUNKED" || name == "Q4T_GDN_SPLIT" ||
         name == "Q4T_MOE_BATCH_GATHER" || name == "Q4T_MOE_STREAMS";
}

bool MatchesMtpReferenceConfiguration(const ServerOptions& effective,
                                      bool mtp_loaded, bool scheduler_ready,
                                      int k, bool experimental_overrides) {
  return !effective.no_mtp && mtp_loaded && scheduler_ready && k == 3 &&
         EffectiveMtpVerifier(effective) == MtpVerifier::kSequential &&
         !effective.allow_media && effective.max_seq == 1 &&
         effective.max_len == 208896 && effective.max_prefill == 8192 &&
         !effective.no_budget && !experimental_overrides;
}

Status ValidateServerOptions(const ServerOptions& options) {
  in_addr address{};
  if (inet_pton(AF_INET, options.host.c_str(), &address) != 1)
    return Status::Fail("host must be a numeric IPv4 address");
  if (options.port < 1 || options.port > 65535)
    return Status::Fail("port must be in [1,65535]");
  if (options.model_dir.empty()) return Status::Fail("model-dir is empty");
  if (options.mtp_verifier && options.no_mtp)
    return Status::Fail("--mtp-verifier requires --mtp");
  if (options.mtp_verifier && *options.mtp_verifier != MtpVerifier::kT4 &&
      *options.mtp_verifier != MtpVerifier::kSequential)
    return Status::Fail("invalid MTP verifier");
  if (options.max_tokens < 1)
    return Status::Fail("max-tokens must be positive");
  if (options.max_prefill < 0 || options.max_len < 0)
    return Status::Fail("max-prefill and max-len must be nonnegative");
  // FullAttentionForward supports at most 8192 tokens per call. Enforce its
  // bound before workspace sizing, whose dimensions include int products.
  // Zero retains ModelConfig's default prefill size.
  if (options.max_prefill > 8192)
    return Status::Fail("max-prefill must be in [0,8192]");
  if (!options.no_mtp &&
      EffectiveMtpVerifier(options) == MtpVerifier::kSequential &&
      options.max_prefill > 0 && options.max_prefill < 4)
    return Status::Fail("sequential MTP requires max-prefill >= 4");
  // The connection cap uses max_seq * 8 in the existing server.
  if (options.max_seq < 1 ||
      options.max_seq > std::numeric_limits<int>::max() / 8)
    return Status::Fail("max-seq must be in [1,268435455]");
  if (!options.no_mtp && (options.max_seq != 1 || options.allow_media))
    return Status::Fail("MTP requires --max-seq 1 and text-only input");
  if (!std::isfinite(options.mem_fraction) || options.mem_fraction <= 0 ||
      options.mem_fraction > 1)
    return Status::Fail("mem-fraction must be finite and in (0,1]");
  if (options.moe_trace_dir.empty() != options.moe_trace_workload.empty())
    return Status::Fail("moe-trace-dir and moe-trace-workload are required together");
  if (options.moe_trace_max_mib < 1 || options.moe_trace_max_mib > 4096)
    return Status::Fail("moe-trace-max-mib must be in [1,4096]");
  return Status();
}

Status ParseServerOptions(std::span<const std::string_view> args,
                          ServerOptions* options) {
  ServerOptions parsed = *options;
  for (size_t i = 0; i < args.size(); ++i) {
    const auto key = args[i];
    if (key == "--mtp") {
      parsed.no_mtp = false;
      continue;
    }
    if (key == "--no-mtp") {
      parsed.no_mtp = true;
      continue;
    }
    if (key == "--allow-media") {
      parsed.allow_media = true;
      continue;
    }
    if (key == "--no-budget") {
      parsed.no_budget = true;
      continue;
    }
    int* integer = nullptr;
    if (key == "--port") integer = &parsed.port;
    if (key == "--max-tokens") integer = &parsed.max_tokens;
    if (key == "--max-prefill") integer = &parsed.max_prefill;
    if (key == "--max-len") integer = &parsed.max_len;
    if (key == "--max-seq") integer = &parsed.max_seq;
    if (key == "--moe-trace-max-mib") integer = &parsed.moe_trace_max_mib;
    if (!integer && key != "--host" && key != "--model-dir" &&
        key != "--mem-fraction" && key != "--moe-trace-dir" &&
        key != "--moe-trace-workload" && key != "--mtp-verifier")
      return Status::Fail("unknown serve option: " + std::string(key));
    if (++i == args.size() || args[i].empty() || args[i].starts_with("--"))
      return Status::Fail("missing value for " + std::string(key));
    const auto value = args[i];
    if (key == "--mtp-verifier") {
      if (value == "t4") {
        parsed.mtp_verifier = MtpVerifier::kT4;
      } else if (value == "sequential") {
        parsed.mtp_verifier = MtpVerifier::kSequential;
      } else {
        return Status::Fail("--mtp-verifier must be sequential or t4");
      }
    } else if (key == "--host") {
      parsed.host = value;
    } else if (key == "--moe-trace-dir") {
      parsed.moe_trace_dir = value;
    } else if (key == "--moe-trace-workload") {
      parsed.moe_trace_workload = value;
    } else if (key == "--model-dir") {
      parsed.model_dir = value;
    } else {
      const char* end = value.data() + value.size();
      const auto result =
          integer ? std::from_chars(value.data(), end, *integer)
                  : std::from_chars(value.data(), end, parsed.mem_fraction);
      if (result.ec != std::errc() || result.ptr != end)
        return Status::Fail("invalid numeric value for " + std::string(key));
    }
  }
  Status status = ValidateServerOptions(parsed);
  if (!status.ok()) return status;
  *options = std::move(parsed);
  return Status();
}

}  // namespace q4t::server
