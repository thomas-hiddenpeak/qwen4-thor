#include "chat_server_internal.h"
#include "q4t/server/cache_health.h"

#include "q4t/server/chat_response_parser.h"
#include "q4t/server/chat_file_inputs.h"
#include "q4t/server/media_admission.h"
#include "q4t/server/request_admission.h"
#include "q4t/server/reasoning_options.h"
#include "q4t/server/request_json.h"
#include "q4t/server/structured_decoder.h"
#include "q4t/server/thinking_budget.h"

namespace q4t::server {

ChatPlanConfig ChatServer::PlanningConfig(int fd) const {
  const auto* http = detail::HttpResponseContext::Current(fd);
  const auto policy = clients_.Snapshot();
  return {.model = model_name_,
          .max_len = max_len_,
          .max_seq = max_seq_,
          .max_prefill = max_prefill_,
          .default_max_tokens = max_tokens_default_,
          .token_vocab = std::min(static_cast<int>(tok_->VocabSize()),
                                  model_.Get().cfg.vocab),
          .request_deadline_ms = request_deadline_ms_,
          .admission = admission_limits_,
          .allow_media = allow_media_,
          .media_attention_shared_bytes =
              vision_device_budget_.attention_shared_bytes,
          .structured = kStructuredDecodingAvailable,
          .session_cache = session_cache_.Enabled(),
          .prefix_cache = prefix_cache_.Enabled(),
          .capacities = seq_capacities_,
          .checkpoints = prefix_cache_checkpoints_,
          .cuda_quota_limit_bytes = cuda_quota_limit_bytes_,
          .client_policy_sha256 = policy ? policy->sha256 : std::string(),
          .client_policy_content_sha256 =
              policy ? ClientPolicyDigest(*policy) : std::string(),
          .client_policy_revision = policy ? policy->revision : 0,
          .client_count = policy ? policy->clients.size() : 0,
          .client_policy_version = policy ? policy->version : 1,
          .client_fair_queue = policy && policy->fair_queue,
          .client_limits = http ? http->Client().Limits() : nullptr,
          .model_aliases = model_catalog_.Aliases(),
          .access_program_policy = access_program_policy_,
          .file_inputs = file_inputs_,
          .file_input_policy_sha256 = file_input_policy_sha256_};
}

void ChatServer::HandleConfig(int fd) {
  const auto health =
      objects_ ? objects_->Health() : ObjectStoreHealth{"unconfigured"};
  const auto jobs = background_ ? background_->Health() : ResponsesJobsHealth{};
  const auto jobs_state = !background_    ? "unavailable"
                          : jobs.failed   ? "failed"
                          : jobs.stopping ? "stopping"
                                          : "ready";
  const auto value =
      effective_config_.substr(0, effective_config_.size() - 1) +
      R"(,"application_state_health":{"state":")" +
      detail::JsonEscape(health.state) + '"' + R"(,"key_admin_ready":)" +
      (state_key_admin_ && state_key_admin_->Alive() ? "true" : "false") +
      R"(,"key_admin_readiness_dependency":false,"pending":)" +
      std::to_string(health.pending) + R"(,"workers_started":)" +
      std::to_string(health.workers_started) + R"(,"workers_reaped":)" +
      std::to_string(health.workers_reaped) + R"(,"controller_starts":)" +
      std::to_string(health.controller_starts) + R"(,"controller_failures":)" +
      std::to_string(health.controller_failures) +
      R"(,"controller_recoveries":)" +
      std::to_string(health.controller_recoveries) +
      R"(,"auxiliary_cleanup_failures":)" +
      std::to_string(health.auxiliary_cleanup_failures) + "}," +
      R"("responses_background":{"state":")" + jobs_state +
      R"(","readiness_dependency":false,"owned":)" +
      std::to_string(jobs.owned) + R"(,"queued":)" +
      std::to_string(jobs.queued) + R"(,"running":)" +
      std::to_string(jobs.running) + R"(,"uncertain":)" +
      std::to_string(jobs.uncertain) + R"(,"input_bytes":)" +
      std::to_string(jobs.input_bytes) + R"(,"recovery_configured":)" +
      (jobs.recovery_configured ? "true" : "false") +
      R"(,"recovery_scanned":)" + (jobs.recovery_scanned ? "true" : "false") +
      R"(,"recovery_pending":)" + std::to_string(jobs.recovery_pending) +
      R"(,"recovered":)" + std::to_string(jobs.recovered) +
      R"(,"interrupted":)" + std::to_string(jobs.interrupted) +
      R"(,"recovery_errors":)" + std::to_string(jobs.recovery_errors) + "}," +
      R"("responses_reconciliation":{"state":")" + jobs_state +
      R"(","readiness_dependency":false,"owned":)" +
      std::to_string(jobs.foreground_owned) + R"(,"active":)" +
      std::to_string(jobs.foreground_active) + R"(,"pending":)" +
      std::to_string(jobs.foreground_pending) + R"(,"reconciled":)" +
      std::to_string(jobs.foreground_reconciled) + R"(,"errors":)" +
      std::to_string(jobs.foreground_errors) + R"(},"cache_io":{"session":)" +
      CacheIoHealthJson(session_cache_.Health()) + R"(,"prefix":)" +
      CacheIoHealthJson(prefix_cache_.Health()) + "}}";
  const auto current = value.substr(0, value.size() - 1) +
      R"(,"current_client_policy":)" +
      DescribeClientPolicy(clients_, admission_limits_.inflight,
                           admission_limits_.queued,
                           client_policy_admin_ &&
                               client_policy_admin_->PublisherAlive()) + '}';
  detail::SendSimple(fd, 200, "OK", current, "application/json");
}

void ChatServer::HandlePreflight(int fd, const std::string& body) {
  const auto arrived = std::chrono::steady_clock::now();
  if (RejectUnhealthy(fd)) return;
  if (!CheckMemoryAdmission(fd, MemoryCheckpoint::kPrepare)) return;
  RequestAdmissionLease lease(active_chats_, admission_limits_.inflight);
  if (!lease.TryAcquire()) {
    metrics_.text_admission_rejected.fetch_add(1, std::memory_order_relaxed);
    detail::SendError(fd, 503, "text request capacity exhausted", {},
                      "capacity_exhausted");
    return;
  }
  io::Json envelope;
  auto status = io::ParseJson(body, &envelope, true, kSchemaJsonLimits);
  if (!status) {
    detail::SendError(fd, 400, "invalid JSON: " + status.message(), {},
                      "invalid_json");
    return;
  }
  ChatPlanError error;
  status = ValidatePreflightEnvelope(envelope, &error);
  const auto send_error = [&] {
    detail::SendError(fd, error.http_status, status.message(), {}, error.code,
                      error.param);
  };
  if (!status) {
    send_error();
    return;
  }
  const auto& original_request = *envelope.Find("request");
  const auto config = PlanningConfig(fd);
  auto validation_config = config;
  validation_config.preparing_file_inputs = true;
  ChatRequestPlan plan;
  status = PlanChatRequest(original_request, validation_config, &plan, &error);
  if (!status) {
    send_error();
    return;
  }
  RequestControl control(
      "preflight", "",
      arrived + std::chrono::milliseconds(original_request.GetInt(
                    "request_timeout_ms", request_deadline_ms_)));
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control.Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(&control, fd);
  };
  const auto send_cancel = [&] {
    detail::SendError(fd, 409, "preflight cancelled", {}, control.Reason());
  };
  const auto* http = detail::HttpResponseContext::Current(fd);
  const BinaryAccess access{http && http->Client().Scoped()
                                ? "client:" + std::string(http->Client().Id())
                                : "local:anonymous",
                            control.deadline(), cancelled};
  ChatFileInputs files(objects_.get());
  const auto prepared = files.Prepare(original_request, config, access, &plan);
  if (!prepared.ok()) {
    detail::SendError(fd, prepared.http_status, prepared.ClientMessage(), {},
                      prepared.ClientCode(), prepared.value.GetString("param"));
    return;
  }
  const auto& req = plan.Request(original_request);
  std::string prompt;
  std::vector<VisionItem> items;
  status = PrepareChatPrompt(req, plan.constraints, &prompt, &items,
                             &error.param, &error.code);
  if (status) {
    ChatResponseParser parser;
    status = parser.Init(req, prompt, "preflight", true, plan.legacy_output);
  }
  if (status) {
    ReasoningOptions options;
    status = ParseReasoningOptions(req, &options, &error.param);
    ThinkingBudget thinking;
    const auto* tools = req.GetArray("tools");
    if (status)
      status = thinking.Init(
          options.token_budget, prompt, plan.constraints.json_output,
          (tools && !tools->array.empty()) ||
              plan.constraints.choice == OutputConstraints::ToolChoice::kNone);
  }
  if (!status) {
    send_error();
    return;
  }
  if (cancelled()) {
    send_cancel();
    return;
  }
  std::vector<uint32_t> tokens;
  ChatPromptBudget budget;
  vision::MediaPlan media;
  {
    auto lock = LockUnlessCancelled(tok_mu_, cancelled);
    if (!lock.owns_lock()) {
      send_cancel();
      return;
    }
    status = EncodeChatPrompt(
        req, config, prompt,
        [&](std::string_view text, std::vector<uint32_t>* out) {
          return tok_->Encode(text, out, {.cancelled = cancelled});
        },
        &tokens, &budget, &error);
  }
  if (status && !items.empty()) {
    const auto& model_config = model_.Get().cfg;
    status = ExpandChatMedia(req, items, model_config.image_token_id,
                             model_config.video_token_id, proc_cfg_,
                             video_proc_cfg_, vision_device_budget_, config,
                             &tokens, &media, &budget, &error);
  }
  if (cancelled()) {
    send_cancel();
    return;
  }
  std::string response;
  if (status)
    status = ChatPreflightJson(envelope, config, plan, budget, prompt, tokens,
                               &response, &error, &media, files.Usage());
  if (!status) {
    send_error();
    return;
  }
  if (cancelled() || !control.Finish()) {
    send_cancel();
    return;
  }
  detail::SendSimple(fd, 200, "OK", response, "application/json",
                     {control.deadline()});
}

}  // namespace q4t::server
