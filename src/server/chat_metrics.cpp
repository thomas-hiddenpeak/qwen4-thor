// Existing ChatServer responsibilities; shared state stays in ChatServer.
#include "chat_server_internal.h"

#include <ctime>
#include <string>

#include "q4t/server/chat_capabilities.h"

namespace q4t::server {
using detail::SendSimple;

namespace {
// le boundaries (seconds) shared by MetricHistogram::Observe and /metrics.
constexpr double kLatencyBounds[MetricHistogram::kNumBuckets] = {
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0,
    2.5,   5.0,  10.0,  30.0, 60.0, 120.0};

}

void MetricHistogram::Observe(double seconds) {
  int i = 0;
  while (i < kNumBuckets && seconds > kLatencyBounds[i]) ++i;
  if (i < kNumBuckets)
    bucket[i].fetch_add(1, std::memory_order_relaxed);
  else
    inf.fetch_add(1, std::memory_order_relaxed);
  count.fetch_add(1, std::memory_order_relaxed);
  if (seconds > 0)
    sum_us.fetch_add(static_cast<uint64_t>(seconds * 1e6),
                     std::memory_order_relaxed);
}

void ChatServer::HandleMetrics(int fd) {
  std::string b;
  b.reserve(2048);
  auto counter = [&](const char* name, const char* help, uint64_t v) {
    b += "# HELP "; b += name; b += ' '; b += help; b += "\n# TYPE ";
    b += name; b += " counter\n"; b += name; b += ' ';
    b += std::to_string(v); b += '\n';
  };
  auto gauge = [&](const char* name, const char* help, long v) {
    b += "# HELP "; b += name; b += ' '; b += help; b += "\n# TYPE ";
    b += name; b += " gauge\n"; b += name; b += ' ';
    b += std::to_string(v); b += '\n';
  };
  auto histogram = [&](const char* name, const char* help,
                       const MetricHistogram& h) {
    b += "# HELP "; b += name; b += ' '; b += help; b += "\n# TYPE ";
    b += name; b += " histogram\n";
    uint64_t cum = 0;
    for (int i = 0; i < MetricHistogram::kNumBuckets; ++i) {
      cum += h.bucket[i].load(std::memory_order_relaxed);
      b += name; b += "_bucket{le=\""; b += std::to_string(kLatencyBounds[i]);
      b += "\"} "; b += std::to_string(cum); b += '\n';
    }
    cum += h.inf.load(std::memory_order_relaxed);
    b += name; b += "_bucket{le=\"+Inf\"} "; b += std::to_string(cum);
    b += '\n'; b += name; b += "_sum ";
    b += std::to_string(h.sum_us.load(std::memory_order_relaxed) / 1e6);
    b += '\n'; b += name; b += "_count "; b += std::to_string(cum); b += '\n';
  };

  counter("q4t_requests_total", "Total chat requests received.",
          metrics_.requests_total.load());
  counter("q4t_requests_success_total",
          "Requests with a complete local response write.",
          metrics_.requests_success.load());
  counter("q4t_requests_error_total",
          "Requests rejected or failed during generation.",
          metrics_.requests_error.load());
  counter("q4t_requests_aborted_total",
          "Requests aborted for any cooperative cancellation reason.",
          metrics_.requests_aborted.load());
  b += "# HELP q4t_requests_aborted_by_reason_total Cooperative aborts by "
       "fixed reason.\n"
       "# TYPE q4t_requests_aborted_by_reason_total counter\n";
  for (size_t i = 0; i < kAbortReasons.size(); ++i) {
    b += "q4t_requests_aborted_by_reason_total{reason=\"";
    b += kAbortReasons[i];
    b += "\"} " + std::to_string(metrics_.abort_reasons[i].load()) + '\n';
  }
  counter("q4t_structured_rejected_total",
          "Structured requests rejected by independent admission.",
          metrics_.structured_rejected.load());
  counter("q4t_structured_prepare_failed_total",
          "Vocabulary or grammar preparation failures.",
          metrics_.structured_prepare_failed.load());
  counter("q4t_structured_workers_started_total", "Grammar worker execs.",
          metrics_.structured_workers_started.load());
  counter("q4t_structured_compiles_started_total", "Worker compiler entries.",
          metrics_.structured_compiles_started.load());
  counter("q4t_structured_workers_reaped_total", "Grammar workers reaped.",
          metrics_.structured_workers_reaped.load());
  counter("q4t_structured_compile_timeouts_total", "Hard preparation timeouts.",
          metrics_.structured_compile_timeouts.load());
  counter("q4t_structured_resource_exhausted_total", "Preparation memory limits.",
          metrics_.structured_resource_exhausted.load());
  gauge("q4t_structured_workers_active", "Owned unreaped grammar processes.",
        metrics_.structured_workers_active.load());
  gauge("q4t_structured_worker_max_rss_bytes",
        "Largest reaped worker ru_maxrss; not server or system memory.",
        metrics_.structured_worker_max_rss_bytes.load());
  counter("q4t_prompt_tokens_total", "Total prompt tokens processed.",
          metrics_.prompt_tokens_total.load());
  counter("q4t_generation_tokens_total", "Total tokens generated.",
          metrics_.generation_tokens_total.load());
  counter("q4t_execution_steps_total",
          "Completed prefill, draft and decode execution boundaries.",
          metrics_.execution_steps_total.load());

  int free_slots = 0;
  {
    const std::lock_guard<std::mutex> lock(seq_mu_);
    for (bool f : seq_free_)
      if (f) ++free_slots;
  }
  gauge("q4t_num_requests_running", "In-flight request threads.",
        active_conns_.load());
  gauge("q4t_request_body_bytes", "Reserved chat body bytes (48 MiB cap).",
        body_budget_.Used());
  gauge("q4t_active_chats", "Parsing, queued and generating chat requests.",
        active_chats_.load());
  gauge("q4t_structured_active",
        "Structured leases held through response cleanup (limit 1).",
        metrics_.structured_active.load());
  gauge("q4t_structured_preparing",
        "Requests decoding vocabulary or compiling grammar.",
        metrics_.structured_preparing.load());
  gauge("q4t_structured_vocab_bytes",
        "Conservative decoded vocabulary storage, not compiler RSS.",
        metrics_.structured_vocab_bytes.load());
  gauge("q4t_seq_slots_total", "Sequence-state pool size (max_seq).", max_seq_);
  gauge("q4t_seq_slots_free", "Free sequence-state slots.", free_slots);
  gauge("q4t_gpu_healthy", "1 if no sticky CUDA error observed, else 0.",
        gpu_healthy_.load() ? 1 : 0);

  histogram("q4t_ttft_seconds",
            "Streaming arrival to first content/reasoning/tool delta sent locally.",
            metrics_.ttft_seconds);
  histogram("q4t_target_ready_seconds", "Arrival to first target token ready.",
            metrics_.target_ready_seconds);
  histogram("q4t_response_seconds", "Arrival to complete local response write.",
            metrics_.response_seconds);
  histogram("q4t_generation_complete_seconds",
            "Arrival to generation exit; excludes final validation and "
            "response write.",
            metrics_.e2e_seconds);
  histogram("q4t_e2e_seconds",
            "Deprecated alias for q4t_generation_complete_seconds, not full "
            "response latency.",
            metrics_.e2e_seconds);
  histogram("q4t_admission_seconds",
            "Arrival to acquired slot, including preprocessing.",
            metrics_.queue_seconds);
  histogram(
      "q4t_queue_seconds",
      "Deprecated alias for q4t_admission_seconds, includes preprocessing.",
      metrics_.queue_seconds);
  histogram("q4t_slot_wait_seconds",
            "Time inside slot allocation, including cancelled waits.",
            metrics_.slot_wait_seconds);
  histogram("q4t_structured_prepare_seconds",
            "Vocabulary and grammar preparation, including failed attempts.",
            metrics_.structured_prepare_seconds);

  SendSimple(fd, 200, "OK", b, "text/plain; version=0.0.4");
}

void ChatServer::HandleHealth(int fd) {
  int free_slots = 0;
  {
    const std::lock_guard<std::mutex> lock(seq_mu_);
    for (bool f : seq_free_)
      if (f) ++free_slots;
  }
  const bool healthy = gpu_healthy_.load(std::memory_order_relaxed);
  const std::string body =
      std::string("{\"status\":\"") + (healthy ? "ok" : "unhealthy") +
      "\",\"gpu_healthy\":" + (healthy ? "true" : "false") +
      ",\"requests_running\":" + std::to_string(active_conns_.load()) +
      ",\"seq_slots_free\":" + std::to_string(free_slots) +
      ",\"seq_slots_total\":" + std::to_string(max_seq_) + "}";
  SendSimple(fd, healthy ? 200 : 503, healthy ? "OK" : "Service Unavailable",
             body, "application/json");
}

void ChatServer::HandleModels(int fd) {
  const std::string body =
      "{\"object\":\"list\",\"data\":[{\"id\":\"" + model_name_ +
      "\",\"object\":\"model\",\"created\":" + std::to_string(std::time(nullptr)) +
      ",\"owned_by\":\"q4t\"}]}";
  SendSimple(fd, 200, "OK", body, "application/json");
}

void ChatServer::HandleCapabilities(int fd) {
  SendSimple(fd, 200, "OK",
             ChatCapabilitiesJson(model_name_, allow_media_, mtp_loaded_),
             "application/json");
}

}  // namespace q4t::server
