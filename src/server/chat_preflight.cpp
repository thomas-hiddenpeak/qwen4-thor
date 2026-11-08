#include "chat_server_internal.h"

#include "q4t/server/chat_response_parser.h"
#include "q4t/server/media_admission.h"
#include "q4t/server/request_admission.h"
#include "q4t/server/reasoning_options.h"
#include "q4t/server/request_json.h"
#include "q4t/server/structured_decoder.h"
#include "q4t/server/thinking_budget.h"

namespace q4t::server {

ChatPlanConfig ChatServer::PlanningConfig(int fd) const {
  const auto* http = detail::HttpResponseContext::Current(fd);
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
          .client_policy_sha256 = clients_.PolicySha256(),
          .client_count = clients_.Size(),
          .client_policy_version = clients_.Version(),
          .client_fair_queue = clients_.FairQueue(),
          .client_limits = http ? http->Client().Limits() : nullptr};
}

void ChatServer::HandleConfig(int fd) {
  detail::SendSimple(fd, 200, "OK", effective_config_, "application/json");
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
  auto status = io::ParseJson(body, &envelope, true, kRequestJsonLimits);
  if (!status) {
    detail::SendError(fd, 400, "invalid JSON: " + status.message(), {},
                      "invalid_json");
    return;
  }
  ChatPlanError error;
  status = ValidatePreflightEnvelope(envelope, &error);
  const auto send_error = [&] {
    detail::SendError(fd, 400, status.message(), {}, error.code, error.param);
  };
  if (!status) {
    send_error();
    return;
  }
  const auto& original_request = *envelope.Find("request");
  const auto config = PlanningConfig(fd);
  ChatRequestPlan plan;
  status = PlanChatRequest(original_request, config, &plan, &error);
  if (!status) {
    send_error();
    return;
  }
  const auto& req = plan.Request(original_request);
  RequestControl control(
      "preflight", "",
      arrived + std::chrono::milliseconds(
                    req.GetInt("request_timeout_ms", request_deadline_ms_)));
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control.Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(&control, fd);
  };
  const auto send_cancel = [&] {
    detail::SendError(fd, 409, "preflight cancelled", {}, control.Reason());
  };
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
                               &response, &error, &media);
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
