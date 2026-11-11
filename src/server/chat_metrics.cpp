// Existing ChatServer responsibilities; shared state stays in ChatServer.
#include "chat_server_internal.h"

#include <ctime>
#include <string>

#include "q4t/server/chat_capabilities.h"
#ifdef Q4T_STARTUP_RESERVATION
#include "q4t/server/cuda_quota_metrics.h"
#endif

namespace q4t::server {
using detail::SendSimple;

namespace {
// le boundaries (seconds) shared by MetricHistogram::Observe and /metrics.
constexpr double kLatencyBounds[MetricHistogram::kNumBuckets] = {
    0.005, 0.01, 0.025, 0.05, 0.1,  0.25, 0.5,
    1.0,   2.5,  5.0,   10.0, 30.0, 60.0, 120.0};

}  // namespace

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
  const auto memory = memory_admission_.Observe();
  std::string b;
  b.reserve(2048);
  b += ClientPolicyMetrics(clients_);
#ifdef Q4T_STARTUP_RESERVATION
  b += CudaQuotaMetrics(runtime::ReadCudaAllocationQuota());
#endif
  auto counter = [&](const char* name, const char* help, uint64_t v) {
    b += "# HELP ";
    b += name;
    b += ' ';
    b += help;
    b += "\n# TYPE ";
    b += name;
    b += " counter\n";
    b += name;
    b += ' ';
    b += std::to_string(v);
    b += '\n';
  };
  auto gauge = [&](const char* name, const char* help, long v) {
    b += "# HELP ";
    b += name;
    b += ' ';
    b += help;
    b += "\n# TYPE ";
    b += name;
    b += " gauge\n";
    b += name;
    b += ' ';
    b += std::to_string(v);
    b += '\n';
  };
  auto histogram = [&](const char* name, const char* help,
                       const MetricHistogram& h) {
    b += "# HELP ";
    b += name;
    b += ' ';
    b += help;
    b += "\n# TYPE ";
    b += name;
    b += " histogram\n";
    uint64_t cum = 0;
    for (int i = 0; i < MetricHistogram::kNumBuckets; ++i) {
      cum += h.bucket[i].load(std::memory_order_relaxed);
      b += name;
      b += "_bucket{le=\"";
      b += std::to_string(kLatencyBounds[i]);
      b += "\"} ";
      b += std::to_string(cum);
      b += '\n';
    }
    cum += h.inf.load(std::memory_order_relaxed);
    b += name;
    b += "_bucket{le=\"+Inf\"} ";
    b += std::to_string(cum);
    b += '\n';
    b += name;
    b += "_sum ";
    b += std::to_string(h.sum_us.load(std::memory_order_relaxed) / 1e6);
    b += '\n';
    b += name;
    b += "_count ";
    b += std::to_string(cum);
    b += '\n';
  };

  gauge("q4t_memory_admission_enabled", "Global memory admission enabled.",
        memory.state != MemoryState::kDisabled);
  gauge("q4t_memory_observation_valid", "Validity of the latest observation.",
        memory.memory.valid);
  gauge("q4t_memory_available_bytes",
        "Linux global MemAvailable estimate; zero when observation invalid.",
        memory.memory.available_bytes);
  gauge("q4t_memory_admission_floor_bytes", "Admission closing threshold.",
        memory.floor_bytes);
  gauge("q4t_memory_admission_recovery_bytes", "Admission reopening threshold.",
        memory.recovery_bytes);
  gauge("q4t_memory_admission_open", "New work allowed by memory admission.",
        memory.Allowed());
  counter("q4t_memory_observations_total",
          "Actual memory samples, not queries.", memory.observations);
  counter("q4t_memory_observation_failures_total", "Invalid memory samples.",
          memory.failures);
  counter("q4t_memory_admission_transitions_total", "Memory gate transitions.",
          memory.transitions);
  b += "# HELP q4t_memory_admission_state Current fixed memory state.\n"
       "# TYPE q4t_memory_admission_state gauge\n";
  for (size_t i = 0; i < kMemoryStates.size(); ++i)
    b += std::string("q4t_memory_admission_state{state=\"") + kMemoryStates[i] +
         "\"} " + (i == static_cast<size_t>(memory.state) ? "1\n" : "0\n");
  b += "# HELP q4t_memory_rejected_total Requests rejected by memory "
       "admission.\n"
       "# TYPE q4t_memory_rejected_total counter\n";
  for (size_t point = 0; point < kMemoryCheckpoints.size(); ++point) {
    for (size_t reason = 0; reason < kMemoryRejectionReasons.size(); ++reason) {
      b += std::string("q4t_memory_rejected_total{checkpoint=\"") +
           kMemoryCheckpoints[point] + "\",reason=\"" +
           kMemoryRejectionReasons[reason] + "\"} " +
           std::to_string(metrics_.memory_rejected[point][reason].load()) +
           '\n';
    }
  }

  counter("q4t_requests_total", "Total chat requests received.",
          metrics_.requests_total.load());
  b += "# HELP q4t_http_receive_failures_total First transport/route rejection "
       "per connection; excludes business validation.\n"
       "# TYPE q4t_http_receive_failures_total counter\n";
  for (size_t i = 1; i < kHttpReceiveReasons.size(); ++i) {
    b += "q4t_http_receive_failures_total{reason=\"";
    b += kHttpReceiveReasons[i];
    b += "\"} " + std::to_string(metrics_.http_transport.receive[i].load()) +
         '\n';
  }
  b += "# HELP q4t_http_write_failures_total First failed logical write per "
       "connection, published after heartbeat join; not task outcomes.\n"
       "# TYPE q4t_http_write_failures_total counter\n";
  for (size_t origin = 0; origin < kHttpWriteOrigins.size(); ++origin) {
    for (size_t reason = 1; reason < kHttpWriteReasons.size(); ++reason) {
      b += "q4t_http_write_failures_total{origin=\"";
      b += kHttpWriteOrigins[origin];
      b += "\",reason=\"";
      b += kHttpWriteReasons[reason];
      b +=
          "\"} " +
          std::to_string(metrics_.http_transport.write[origin][reason].load()) +
          '\n';
    }
  }
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
  detail::PollRequestWorkers();
  gauge("q4t_structured_cleanup_pending",
        "Unconfirmed cleanup retains the process-wide structured capacity.",
        detail::GrammarCleanupPending() ? 1 : 0);
  counter("q4t_structured_workers_started_total", "Grammar worker execs.",
          metrics_.structured_workers->started.load());
  counter("q4t_structured_workers_ready_total",
          "Workers with limits installed.",
          metrics_.structured_workers_ready.load());
  counter("q4t_structured_compiles_started_total", "Worker compiler entries.",
          metrics_.structured_compiles_started.load());
  counter("q4t_structured_workers_reaped_total", "Grammar workers reaped.",
          metrics_.structured_workers->reaped.load());
  counter("q4t_structured_compile_timeouts_total", "Hard preparation timeouts.",
          metrics_.structured_compile_timeouts.load());
  counter("q4t_structured_resource_exhausted_total",
          "Preparation memory limits.",
          metrics_.structured_resource_exhausted.load());
  gauge("q4t_structured_workers_active", "Owned unreaped grammar processes.",
        metrics_.structured_workers->active.load());
  gauge("q4t_structured_worker_lifetime_max_rss_bytes",
        "wait4 ru_maxrss includes pre-exec inherited mm; not AS-limited peak.",
        metrics_.structured_workers->max_rss_bytes.load());
  gauge("q4t_structured_worker_reported_rss_bytes",
        "Maximum reported post-exec VmHWM; may omit tail before abrupt kill.",
        metrics_.structured_workers->reported_rss_bytes.load());
  counter("q4t_structured_worker_rss_reports_total",
          "Reaped workers with a valid post-exec VmHWM report.",
          metrics_.structured_workers->rss_reports.load());
  counter("q4t_prompt_tokens_total", "Total prompt tokens processed.",
          metrics_.prompt_tokens_total.load());
  counter("q4t_generation_tokens_total", "Total tokens generated.",
          metrics_.generation_tokens_total.load());
  counter("q4t_reasoning_budget_tokens_total",
          "Natural tokens charged to thinking budgets.",
          metrics_.reasoning_budget_tokens_total.load());
  counter("q4t_reasoning_forced_tokens_total",
          "Generated UTF-8/closure control tokens.",
          metrics_.reasoning_forced_tokens_total.load());
  counter("q4t_reasoning_forced_requests_total",
          "Requests emitting forced thinking closure.",
          metrics_.reasoning_forced_requests.load());
  counter("q4t_reasoning_mtp_fallbacks_total",
          "MTP requests entering a budget-controlled tail.",
          metrics_.reasoning_mtp_fallbacks.load());
  counter("q4t_execution_steps_total",
          "Completed prefill, draft and decode execution boundaries.",
          metrics_.execution_steps_total.load());

  int free_slots = 0;
  size_t queued_requests = 0;
  {
    const std::lock_guard<std::mutex> lock(seq_mu_);
    queued_requests = seq_waiters_.Size();
    for (bool f : seq_free_)
      if (f) ++free_slots;
  }
  gauge("q4t_num_requests_running", "In-flight request threads.",
        active_conns_.load());
  gauge("q4t_public_connections",
        "Public listener handlers, including incomplete headers.",
        public_conns_.load());
  gauge("q4t_operations_listener_enabled",
        "Independent local listener enabled.", operations_port_ != 0 ? 1 : 0);
  gauge("q4t_operations_connections",
        "Operations handlers, including incomplete headers (limit 8).",
        operations_conns_.load());
  gauge("q4t_operations_observation_connections",
        "Operations observation connections (limit 4).",
        operations_observation_conns_.load());
  gauge("q4t_operations_control_connections",
        "Operations control connections (limit 4).",
        operations_control_conns_.load());
  gauge("q4t_request_body_bytes", "Reserved application body bytes (48 MiB cap).",
        body_budget_.Used());
  gauge("q4t_cancel_body_bytes", "Reserved cancellation bytes (32 MiB cap).",
        cancel_body_budget_.Used());
  gauge("q4t_active_chats", "Admitted chat/preflight through output cleanup.",
        active_chats_.load());
  gauge("q4t_queued_requests", "Slot waiters, excluding preprocessing.",
        queued_requests);
  counter("q4t_queue_rejected_total", "Requests rejected at the slot queue.",
          metrics_.queue_rejected.load());
  counter("q4t_text_admission_rejected_total",
          "Chat/preflight rejected at the shared inflight limit.",
          metrics_.text_admission_rejected.load());
  gauge("q4t_business_connections", "Classified business-route connections.",
        business_conns_.load());
  gauge("q4t_observation_connections",
        "Public listener observation connections (limit 4).",
        observation_conns_.load());
  b += "# HELP q4t_connection_rejections_total Connection capacity refusals "
       "by fixed partition.\n"
       "# TYPE q4t_connection_rejections_total counter\n";
  for (size_t i = 0; i < kHttpConnectionClasses.size(); ++i)
    b += "q4t_connection_rejections_total{kind=\"" +
         std::string(kHttpConnectionClasses[i]) + "\"} " +
         std::to_string(metrics_.connection_rejections[i].load()) + '\n';
  b += "# HELP q4t_operations_connection_rejections_total Operations listener "
       "capacity refusals by fixed partition.\n"
       "# TYPE q4t_operations_connection_rejections_total counter\n";
  for (size_t i = 0; i < kHttpConnectionClasses.size(); ++i)
    b += "q4t_operations_connection_rejections_total{kind=\"" +
         std::string(kHttpConnectionClasses[i]) + "\"} " +
         std::to_string(metrics_.operations_connection_rejections[i].load()) +
         '\n';
  gauge("q4t_control_connections", "Classified control connections (limit 4).",
        control_conns_.load());
  gauge("q4t_ready", "Ready for business; ordinary slot occupancy is separate.",
        ObserveHealth(memory.state).Ready() ? 1 : 0);
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

  histogram(
      "q4t_ttft_seconds",
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
  const auto health = ObserveHealth();
  const bool healthy = health.Healthy();
  const std::string body =
      std::string("{\"status\":\"") + (healthy ? "ok" : "unhealthy") +
      "\",\"gpu_healthy\":" + (health.gpu_healthy ? "true" : "false") +
      ",\"requests_running\":" + std::to_string(active_conns_.load()) +
      ",\"seq_slots_free\":" + std::to_string(free_slots) +
      ",\"seq_slots_total\":" + std::to_string(max_seq_) + "}";
  SendSimple(fd, healthy ? 200 : 503, healthy ? "OK" : "Service Unavailable",
             body, "application/json");
}

void ChatServer::HandleModels(int fd) {
  auto body = model_catalog_.ListJson();
  if (!embedding_model_dir_.empty()) {
    body.resize(body.size() - 2);
    body += ',' + embedding_catalog_.RetrieveJson(
                      model::EmbeddingConfig::kModelId) + "]}";
  }
  SendSimple(fd, 200, "OK", body, "application/json");
}

void ChatServer::HandleModel(int fd, const std::string& method,
                             const std::string& path) {
  std::string model;
  const auto decoded = DecodeModelPath(path, &model);
  if (!decoded) {
    detail::SendError(fd, 400, decoded.message(), {}, "invalid_model_path",
                      "model");
  } else if (!model_catalog_.Contains(model) &&
             (embedding_model_dir_.empty() ||
              !embedding_catalog_.Contains(model))) {
    detail::SendError(fd, 404, "model is not registered by this server", {},
                      "model_not_found", "model");
  } else if (method == "DELETE") {
    // The standard delete operation is for user-owned fine-tuned models.
    // This registry owns no such assets; loaded base weights remain read-only.
    detail::SendError(fd, 403, "loaded base models cannot be deleted", {},
                      "model_not_deletable", "model");
  } else {
    const auto& catalog = model_catalog_.Contains(model) ? model_catalog_
                                                        : embedding_catalog_;
    SendSimple(fd, 200, "OK", catalog.RetrieveJson(model),
               "application/json");
  }
}

void ChatServer::HandleCapabilities(int fd) {
  SendSimple(fd, 200, "OK",
             ChatCapabilitiesJson(model_name_, allow_media_, mtp_loaded_,
                                  vision_device_budget_.attention_shared_bytes,
                                  access_program_policy_.get(),
                                  !embedding_model_dir_.empty()),
             "application/json");
}

}  // namespace q4t::server
