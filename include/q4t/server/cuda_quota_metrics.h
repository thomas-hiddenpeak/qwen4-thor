#pragma once

#include <string>

#include "q4t/runtime/cuda_allocation_quota.h"

namespace q4t::server {

// Formatting a copied snapshot performs no CUDA calls and takes no locks.
inline std::string CudaQuotaMetrics(const runtime::CudaQuotaSnapshot& value) {
  std::string output;
  const auto append = [&](const char* suffix, const char* help, uint64_t number,
                          const char* type = "gauge") {
    const std::string name = std::string("q4t_cuda_quota_") + suffix;
    output += "# HELP " + name + " " + help + "\n# TYPE " + name + " " + type +
              "\n" + name + " " + std::to_string(number) + '\n';
  };
  const auto& m = value.memory;
  append("enabled", "Project-linked requested-byte quota enabled.",
         value.enabled);
  append("limit_bytes", "Requested-byte limit; not physical RAM.",
         m.limit_bytes);
  append("charged_bytes", "All outstanding requested bytes.", m.charged_bytes);
  append("reserved_bytes", "Reserved before backend allocation.",
         m.reserved_bytes);
  append("live_bytes", "Owned requested bytes including process caches.",
         m.live_bytes);
  append("releasing_bytes", "Bytes with release result not yet published.",
         m.releasing_bytes);
  append("pending_bytes", "Async releases awaiting successful synchronization.",
         m.pending_bytes);
  append("unknown_bytes", "Quarantined bytes with unknown ownership.",
         m.unknown_bytes);
  append("peak_bytes", "Maximum charged requested bytes in this process.",
         m.peak_bytes);
  append("entries", "Outstanding allocation metadata entries.", m.entries);
  append("entry_capacity", "Fixed allocation metadata capacity.", 16384);
  append("stream_entries", "Tracked live explicit streams.",
         value.stream_entries);
  append("stream_capacity", "Fixed explicit stream metadata capacity.",
         value.stream_capacity);
  append("poisoned", "Unknown allocation or stream ownership latched.",
         m.poisoned);
  append("byte_rejections_total",
         "Requests rejected before CUDA by byte limit.", value.byte_rejections,
         "counter");
  append("entry_rejections_total", "Requests rejected by allocation table.",
         value.entry_rejections, "counter");
  append("stream_rejections_total", "Requests rejected by stream table.",
         value.stream_rejections, "counter");
  append("contract_failures_total", "Allocation ownership contract failures.",
         value.contract_failures, "counter");
  append("backend_failures_total", "Known backend failures after reservation.",
         value.backend_failures, "counter");
  return output;
}
}  // namespace q4t::server
