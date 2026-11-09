#pragma once

#include <string>

#include "q4t/server/cache_io.h"

namespace q4t::server {
// Only fixed internal states/counters; never directory, session key or content.
inline std::string CacheIoHealthJson(const CacheIoHealth& h) {
  return std::string(R"({"configured":)") + (h.configured ? "true" : "false") +
         R"(,"available":)" + (h.available ? "true" : "false") +
         R"(,"state":")" + std::string(h.state) +
         R"(","readiness_dependency":false,"generation":)" +
         std::to_string(h.generation) + R"(,"workers_started":)" +
         std::to_string(h.workers_started) + R"(,"workers_reaped":)" +
         std::to_string(h.workers_reaped) + R"(,"workers_active":)" +
         std::to_string(h.workers_active) + R"(,"controller_failures":)" +
         std::to_string(h.controller_failures) + R"(,"worker_rss_known":)" +
         (h.worker_rss_bytes ? "true" : "false") +
         R"(,"worker_rss_observed_bytes":)" +
         std::to_string(h.worker_rss_bytes) +
         R"(,"io_timeout_ms":2000,"restart_limit":3,)"
         R"("restart_window_ms":60000,"block_bytes":8388608,)"
         R"("buffer_payload_limit_bytes":25169920,)"
         R"("buffer_limit_excludes_process_overhead":true})";
}
}  // namespace q4t::server
