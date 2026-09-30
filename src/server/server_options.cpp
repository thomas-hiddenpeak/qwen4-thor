#include "q4t/server/server_options.h"

#include <arpa/inet.h>

#include <charconv>
#include <cmath>
#include <limits>

namespace q4t::server {

ServerCapabilities CapabilitiesFor(const ServerOptions& options) {
  return {!options.no_mtp, options.allow_media, options.max_seq > 1};
}

Status ValidateServerOptions(const ServerOptions& options) {
  in_addr address{};
  if (inet_pton(AF_INET, options.host.c_str(), &address) != 1)
    return Status::Fail("host must be a numeric IPv4 address");
  if (options.port < 1 || options.port > 65535)
    return Status::Fail("port must be in [1,65535]");
  if (options.model_dir.empty()) return Status::Fail("model-dir is empty");
  if (options.max_tokens < 1)
    return Status::Fail("max-tokens must be positive");
  if (options.max_prefill < 0 || options.max_len < 0)
    return Status::Fail("max-prefill and max-len must be nonnegative");
  // The connection cap uses max_seq * 8 in the existing server.
  if (options.max_seq < 1 ||
      options.max_seq > std::numeric_limits<int>::max() / 8)
    return Status::Fail("max-seq must be in [1,268435455]");
  if (!std::isfinite(options.mem_fraction) || options.mem_fraction <= 0 ||
      options.mem_fraction > 1)
    return Status::Fail("mem-fraction must be finite and in (0,1]");
  if (options.moe_trace_dir.empty() != options.moe_trace_workload.empty())
    return Status::Fail("moe-trace-dir and moe-trace-workload are required together");
  if (options.moe_trace_max_mib < 1 || options.moe_trace_max_mib > 4096)
    return Status::Fail("moe-trace-max-mib must be in [1,4096]");
  // 512 is the qwen4_exp routed-expert count (architecture constant).
  if (options.moe_resident_slots < 0 || options.moe_resident_slots > 512)
    return Status::Fail("moe-resident-slots must be in [0,512]");
  if (options.moe_hot_protect &&
      (options.moe_resident_slots <= 0 || options.moe_hot_list.empty())) {
    return Status::Fail(
        "moe-hot-protect requires moe-resident-slots > 0 and moe-hot-list");
  }
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
    if (key == "--moe-hot-protect") {
      parsed.moe_hot_protect = true;
      continue;
    }
    int* integer = nullptr;
    if (key == "--port") integer = &parsed.port;
    if (key == "--max-tokens") integer = &parsed.max_tokens;
    if (key == "--max-prefill") integer = &parsed.max_prefill;
    if (key == "--max-len") integer = &parsed.max_len;
    if (key == "--max-seq") integer = &parsed.max_seq;
    if (key == "--moe-trace-max-mib") integer = &parsed.moe_trace_max_mib;
    if (key == "--moe-resident-slots") integer = &parsed.moe_resident_slots;
    if (!integer && key != "--host" && key != "--model-dir" &&
        key != "--mem-fraction" && key != "--moe-trace-dir" &&
        key != "--moe-trace-workload" && key != "--moe-hot-list")
      return Status::Fail("unknown serve option: " + std::string(key));
    if (++i == args.size() || args[i].empty() || args[i].starts_with("--"))
      return Status::Fail("missing value for " + std::string(key));
    const auto value = args[i];
    if (key == "--host") {
      parsed.host = value;
    } else if (key == "--moe-trace-dir") {
      parsed.moe_trace_dir = value;
    } else if (key == "--moe-trace-workload") {
      parsed.moe_trace_workload = value;
    } else if (key == "--model-dir") {
      parsed.model_dir = value;
    } else if (key == "--moe-hot-list") {
      parsed.moe_hot_list = value;
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
