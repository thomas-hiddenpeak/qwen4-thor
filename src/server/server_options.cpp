#include "q4t/server/server_options.h"

#include <arpa/inet.h>

#include <charconv>
#include <cmath>
#include <limits>

#include "q4t/mtp/suffix_drafter.h"
#include "q4t/model/embedding_config.h"
#include "q4t/server/model_catalog.h"
#include "q4t/server/object_store_options.h"

namespace q4t::server {

ServerCapabilities CapabilitiesFor(const ServerOptions& options) {
  return {!options.no_mtp, options.allow_media, options.max_seq > 1};
}

Status ResolveAdmissionLimits(const ServerOptions& options, int effective_slots,
                              AdmissionLimits* output) {
  if (effective_slots < 1 || effective_slots > 8192)
    return Status::Fail("effective slots must be in [1,8192]");
  if (options.max_inflight_requests < 0 ||
      options.max_inflight_requests > 4096 ||
      options.max_queued_requests < -1 || options.max_queued_requests > 4096)
    return Status::Fail("invalid admission request limits");
  AdmissionLimits result;
  result.inflight = options.max_inflight_requests
                        ? options.max_inflight_requests
                        : TextAdmissionLimit(effective_slots);
  result.queued = options.max_queued_requests >= 0
                      ? options.max_queued_requests
                      : std::max(0, result.inflight - effective_slots);
  if (result.queued > result.inflight)
    return Status::Fail("max-queued-requests exceeds effective inflight limit");
  *output = result;
  return Status();
}

Status ValidateServerOptions(const ServerOptions& options) {
  const auto objects = ValidateObjectStoreOptions(options);
  if (!objects) return objects;
  if (!ValidServedModels(options.served_model_name, options.model_aliases))
    return Status::Fail("invalid or duplicate served model name/alias");
  if (options.allow_media &&
      (!options.no_mtp || options.max_seq != 1 || options.no_budget ||
       options.moe_resident_slots != 0 || !options.moe_trace_dir.empty()))
    return Status::Fail(
        "media v1 requires --no-mtp, --max-seq 1, enabled budget and "
        "no expert offload/trace");
  if (options.cuda_allocation_limit_mib < -1 ||
      options.cuda_allocation_limit_mib > 1048576)
    return Status::Fail(
        "cuda-allocation-limit-mib must be auto or [0,1048576]");
  if (options.startup_memory_headroom_mib < 0 ||
      options.startup_memory_headroom_mib > 1048576)
    return Status::Fail("startup memory headroom must be in [0,1048576] MiB");
  if (options.min_available_memory_mib < 0 ||
      options.min_available_memory_mib > 1048576 ||
      options.memory_recovery_margin_mib < 1 ||
      options.memory_recovery_margin_mib > 1048576)
    return Status::Fail(
        "memory floor must be in [0,1048576] MiB and recovery margin in "
        "[1,1048576] MiB");
  if (options.max_inflight_requests < 0 ||
      options.max_inflight_requests > 4096 ||
      options.max_queued_requests < -1 || options.max_queued_requests > 4096)
    return Status::Fail("admission limits must be auto or in [0,4096]");
  if (options.max_inflight_requests > 0 &&
      options.max_queued_requests > options.max_inflight_requests)
    return Status::Fail("max-queued-requests exceeds max-inflight-requests");
  if ((!options.session_cache_dir.empty() ||
       !options.prefix_cache_dir.empty()) &&
      (options.allow_media ||
       (!options.no_mtp &&
        (options.mtp_k != 3 || options.seq_capacities.empty())) ||
       options.moe_resident_slots != 0 || !options.moe_trace_dir.empty()))
    return Status::Fail(
        "state caches require text, no trace/offload; MTP also requires "
        "k=3 and explicit slots");
  if (options.prefix_cache_checkpoints.empty() ||
      options.prefix_cache_checkpoints.size() > 4)
    return Status::Fail("prefix-cache requires 1 to 4 checkpoints");
  int previous = 0;
  for (const int point : options.prefix_cache_checkpoints) {
    if (point < 1024 || point > 131072 || point <= previous)
      return Status::Fail(
          "prefix checkpoints must increase within 1024..131072");
    previous = point;
  }
  if (options.mtp_suffix &&
      (options.no_mtp || options.mtp_k != 3 || options.allow_media ||
       options.max_len > static_cast<int>(mtp::SuffixDrafter::kMaxCapacity)))
    return Status::Fail("mtp-suffix requires text MTP k=3, max-len<=262144");
  if (!options.mtp_subset_head.empty() &&
      (options.no_mtp || options.mtp_k != 3))
    return Status::Fail("mtp-subset-head requires MTP with fixed k=3");
  if (!runtime::ValidSequenceCapacities(options.seq_capacities, options.max_len,
                                        options.max_seq) ||
      (!options.seq_capacities.empty() &&
       (options.allow_media || options.no_budget)))
    return Status::Fail(
        "seq-capacities requires explicit page-aligned text "
        "slots and an enabled budget");
  in_addr address{};
  if (inet_pton(AF_INET, options.host.c_str(), &address) != 1)
    return Status::Fail("host must be a numeric IPv4 address");
  if (!options.client_policy_file.empty() && options.host != "127.0.0.1")
    return Status::Fail("client-policy-file requires loopback serving");
  if (options.port < 1 || options.port > 65535)
    return Status::Fail("port must be in [1,65535]");
  if (options.operations_port < 0 || options.operations_port > 65535 ||
      options.operations_port == options.port)
    return Status::Fail(
        "operations-port must be 0 or a distinct port [1,65535]");
  if (!options.access_program_policy_file.empty() &&
      options.client_policy_file.empty())
    return Status::Fail("access-program-policy-file requires client policy");
  if (options.model_dir.empty()) return Status::Fail("model-dir is empty");
  if (!options.embedding_model_dir.empty()) {
    if (!model::ValidEmbeddingAssetDirectory(options.embedding_model_dir))
      return Status::Fail("embedding-model-dir must be an absolute asset path");
    if (IsServedModel(model::EmbeddingConfig::kModelId,
                      options.served_model_name, options.model_aliases))
      return Status::Fail("embedding model name conflicts with Chat model names");
  }
  if (options.file_input_policy_file.size() > 4096 ||
      options.file_input_policy_file.find('\0') != std::string::npos)
    return Status::Fail("invalid file-input-policy-file path");
  if (options.max_tokens < 1)
    return Status::Fail("max-tokens must be positive");
  if (options.max_prefill < 0 || options.max_len < 0)
    return Status::Fail("max-prefill and max-len must be nonnegative");
  // FullAttentionForward supports at most 8192 tokens per call. Enforce its
  // bound before workspace sizing, whose dimensions include int products.
  // Zero retains ModelConfig's default prefill size.
  if (options.max_prefill > 8192)
    return Status::Fail("max-prefill must be in [0,8192]");
  // The connection cap uses max_seq * 8 in the existing server.
  if (options.max_seq < 1 ||
      options.max_seq > std::numeric_limits<int>::max() / 8)
    return Status::Fail("max-seq must be in [1,268435455]");
  const int forward_rows = options.max_prefill > 0
                               ? options.max_prefill
                               : runtime::kDefaultMaxPrefill;
  if (options.max_seq > forward_rows)
    return Status::Fail("max-seq exceeds effective max-prefill forward rows");
  if (!std::isfinite(options.mem_fraction) || options.mem_fraction <= 0 ||
      options.mem_fraction > 1)
    return Status::Fail("mem-fraction must be finite and in (0,1]");
  if (options.startup_plan != StartupPlanMode::kAuto &&
      options.startup_plan != StartupPlanMode::kOff &&
      options.startup_plan != StartupPlanMode::kRequired)
    return Status::Fail("startup-plan must be auto, off or required");
  if (options.moe_trace_dir.empty() != options.moe_trace_workload.empty())
    return Status::Fail(
        "moe-trace-dir and moe-trace-workload are required together");
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
  if (options.request_deadline_ms < 1000 ||
      options.request_deadline_ms > 10800000)
    return Status::Fail("request-deadline-ms must be in [1000,10800000]");
  if (options.background_max_input_mib < 1 ||
      options.background_max_input_mib > 2048)
    return Status::Fail("background-max-input-mib must be in [1,2048]");
  if (options.batch_workers < 1 || options.batch_workers > 8 ||
      options.batch_max_jobs < 1 || options.batch_max_jobs > 64 ||
      options.batch_workers > options.batch_max_jobs ||
      options.batch_max_client_jobs < 1 ||
      options.batch_max_client_jobs > options.batch_max_jobs ||
      options.batch_max_result_mib < 96 || options.batch_max_result_mib > 1024)
    return Status::Fail("invalid Batch worker/job/result limits");
  // k=3 is the measured optimum; the verify forward and MTP scratch scale
  // linearly with k+1, so cap experimentation well below kMaxT.
  if (options.mtp_k < 1 || options.mtp_k > 16)
    return Status::Fail("mtp-k must be in [1,16]");
  return Status();
}

Status ParseServerOptions(std::span<const std::string_view> args,
                          ServerOptions* options) {
  ServerOptions parsed = *options;
  for (size_t i = 0; i < args.size(); ++i) {
    const auto key = args[i];
    if (std::find(parsed.supplied_options.begin(),
                  parsed.supplied_options.end(),
                  key) == parsed.supplied_options.end())
      parsed.supplied_options.emplace_back(key);
    if (key == "--mtp") {
      parsed.no_mtp = false;
      continue;
    }
    if (key == "--no-mtp") {
      parsed.no_mtp = true;
      continue;
    }
    if (key == "--mtp-suffix") {
      parsed.mtp_suffix = true;
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
    if (key == "--operations-port") integer = &parsed.operations_port;
    if (key == "--max-tokens") integer = &parsed.max_tokens;
    if (key == "--max-prefill") integer = &parsed.max_prefill;
    if (key == "--max-len") integer = &parsed.max_len;
    if (key == "--max-seq") integer = &parsed.max_seq;
    if (key == "--max-inflight-requests")
      integer = &parsed.max_inflight_requests;
    if (key == "--max-queued-requests") integer = &parsed.max_queued_requests;
    if (key == "--min-available-memory-mib")
      integer = &parsed.min_available_memory_mib;
    if (key == "--memory-recovery-margin-mib")
      integer = &parsed.memory_recovery_margin_mib;
    if (key == "--startup-memory-headroom-mib")
      integer = &parsed.startup_memory_headroom_mib;
    if (key == "--cuda-allocation-limit-mib")
      integer = &parsed.cuda_allocation_limit_mib;
    if (key == "--moe-trace-max-mib") integer = &parsed.moe_trace_max_mib;
    if (key == "--moe-resident-slots") integer = &parsed.moe_resident_slots;
    if (key == "--request-deadline-ms") integer = &parsed.request_deadline_ms;
    if (key == "--background-max-input-mib")
      integer = &parsed.background_max_input_mib;
    if (key == "--batch-workers") integer = &parsed.batch_workers;
    if (key == "--batch-max-jobs") integer = &parsed.batch_max_jobs;
    if (key == "--batch-max-client-jobs")
      integer = &parsed.batch_max_client_jobs;
    if (key == "--batch-max-result-mib") integer = &parsed.batch_max_result_mib;
    uint64_t* wide = nullptr;
    if (key == "--batch-model-queue-tokens")
      wide = &parsed.batch_model_queue_tokens;
    if (key == "--batch-client-model-queue-tokens")
      wide = &parsed.batch_client_model_queue_tokens;
    if (key == "--mtp-k") integer = &parsed.mtp_k;
    if (key == "--object-max-object-mib")
      integer = &parsed.object_max_object_mib;
    if (key == "--object-max-client-mib")
      integer = &parsed.object_max_client_mib;
    if (key == "--object-max-total-mib") integer = &parsed.object_max_total_mib;
    if (key == "--object-max-client-objects")
      integer = &parsed.object_max_client_objects;
    if (key == "--object-max-total-objects")
      integer = &parsed.object_max_total_objects;
    if (key == "--object-io-timeout-ms") integer = &parsed.object_io_timeout_ms;
    if (!integer && !wide && key != "--host" && key != "--model-dir" &&
        key != "--embedding-model-dir" &&
        key != "--mem-fraction" && key != "--moe-trace-dir" &&
        key != "--moe-trace-workload" && key != "--moe-hot-list" &&
        key != "--startup-plan" && key != "--mtp-subset-head" &&
        key != "--seq-capacities" && key != "--session-cache-dir" &&
        key != "--prefix-cache-dir" && key != "--prefix-cache-checkpoints" &&
        key != "--client-policy-file" &&
        key != "--access-program-policy-file" &&
        key != "--file-input-policy-file" && key != "--served-model-name" &&
        key != "--model-alias" && key != "--object-store-dir")
      return Status::Fail("unknown serve option: " + std::string(key));
    if (++i == args.size() || args[i].empty() || args[i].starts_with("--"))
      return Status::Fail("missing value for " + std::string(key));
    const auto value = args[i];
    if (wide) {
      const auto parsed_number =
          std::from_chars(value.data(), value.data() + value.size(), *wide);
      if (parsed_number.ec != std::errc() ||
          parsed_number.ptr != value.data() + value.size())
        return Status::Fail("invalid integer for " + std::string(key));
      continue;
    }
    if (key == "--cuda-allocation-limit-mib") {
      if (value == "auto") {
        parsed.cuda_allocation_limit_mib = -1;
        continue;
      }
      if (value.starts_with('-'))
        return Status::Fail(
            "cuda-allocation-limit-mib must be auto or [0,1048576]");
    }
    if (key == "--seq-capacities" || key == "--prefix-cache-checkpoints") {
      auto& values = key == "--seq-capacities"
                         ? parsed.seq_capacities
                         : parsed.prefix_cache_checkpoints;
      values.clear();
      size_t start = 0;
      while (start < value.size()) {
        const size_t comma = value.find(',', start);
        const size_t end = comma == value.npos ? value.size() : comma;
        int capacity = 0;
        const auto result =
            std::from_chars(value.data() + start, value.data() + end, capacity);
        if (result.ec != std::errc() || result.ptr != value.data() + end ||
            values.size() >= 64 || end + 1 == value.size())
          return Status::Fail("invalid capacity/checkpoint list");
        values.push_back(capacity);
        start = end + 1;
      }
    } else if (key == "--prefix-cache-dir") {
      parsed.prefix_cache_dir = value;
    } else if (key == "--session-cache-dir") {
      parsed.session_cache_dir = value;
    } else if (key == "--object-store-dir") {
      parsed.object_store_dir = value;
    } else if (key == "--mtp-subset-head") {
      parsed.mtp_subset_head = value;
    } else if (key == "--access-program-policy-file") {
      parsed.access_program_policy_file = value;
    } else if (key == "--file-input-policy-file") {
      parsed.file_input_policy_file = value;
    } else if (key == "--client-policy-file") {
      parsed.client_policy_file = value;
    } else if (key == "--host") {
      parsed.host = value;
    } else if (key == "--moe-trace-dir") {
      parsed.moe_trace_dir = value;
    } else if (key == "--moe-trace-workload") {
      parsed.moe_trace_workload = value;
    } else if (key == "--model-dir") {
      parsed.model_dir = value;
    } else if (key == "--embedding-model-dir") {
      parsed.embedding_model_dir = value;
    } else if (key == "--served-model-name") {
      parsed.served_model_name = value;
    } else if (key == "--model-alias") {
      parsed.model_aliases.emplace_back(value);
    } else if (key == "--moe-hot-list") {
      parsed.moe_hot_list = value;
    } else if (key == "--startup-plan") {
      if (value == "auto") {
        parsed.startup_plan = StartupPlanMode::kAuto;
      } else if (value == "off") {
        parsed.startup_plan = StartupPlanMode::kOff;
      } else if (value == "required") {
        parsed.startup_plan = StartupPlanMode::kRequired;
      } else {
        return Status::Fail("startup-plan must be auto, off or required");
      }
    } else {
      const char* end = value.data() + value.size();
      const auto result =
          integer ? std::from_chars(value.data(), end, *integer)
                  : std::from_chars(value.data(), end, parsed.mem_fraction);
      if (result.ec != std::errc() || result.ptr != end)
        return Status::Fail("invalid numeric value for " + std::string(key));
      if ((key == "--max-inflight-requests" && *integer <= 0) ||
          (key == "--max-queued-requests" && *integer < 0))
        return Status::Fail("explicit admission limit out of range: " +
                            std::string(key));
    }
  }
  Status status = ValidateServerOptions(parsed);
  if (!status.ok()) return status;
  *options = std::move(parsed);
  return Status();
}

}  // namespace q4t::server
