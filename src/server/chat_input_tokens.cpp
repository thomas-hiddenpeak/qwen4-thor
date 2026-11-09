#include "chat_server_internal.h"

#include "q4t/server/media_admission.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/request_admission.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_tokens.h"

namespace q4t::server {

void ChatServer::HandleResponsesTokenCount(int fd, const std::string& body) {
  const auto arrived = std::chrono::steady_clock::now();
  if (RejectUnhealthy(fd) ||
      !CheckMemoryAdmission(fd, MemoryCheckpoint::kPrepare))
    return;
  RequestAdmissionLease lease(active_chats_, admission_limits_.inflight);
  if (!lease.TryAcquire()) {
    detail::SendError(fd, 503, "input counting capacity exhausted", {},
                      "capacity_exhausted");
    return;
  }
  const auto* http = detail::HttpResponseContext::Current(fd);
  if (!objects_ || !http || !http->Client().Allowed()) {
    detail::SendError(fd, 503, "Authenticated application state unavailable",
                      {}, "object_scope_unavailable");
    return;
  }
  io::Json request;
  auto status = body.empty()
                    ? Status()
                    : io::ParseJson(body, &request, true, kSchemaJsonLimits);
  if (!status) {
    detail::SendError(fd, 400, "invalid JSON: " + status.message(), {},
                      "invalid_json");
    return;
  }
  const auto& client = http->Client();
  StateAccess access{
      client.Scoped() ? "client:" + std::string(client.Id())
                      : "local:anonymous",
      arrived + std::chrono::milliseconds(request_deadline_ms_), [this, fd] {
        return stop_requested_.load() || detail::ClientConnectionFailed(fd);
      }};
  const auto send = [&](const ObjectReply& error) {
    detail::SendError(fd, error.http_status, error.message, {}, error.code,
                      error.value.GetString("param"));
  };
  const auto cancelled = [&] {
    return !CheckResponsesTokenCountActive(access).ok();
  };
  auto config = PlanningConfig(fd);
  config.count_input_only = true;
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan generation;
  auto prepared = PrepareResponsesTokenCount(request, config, *objects_, access,
                                             &envelope, &generation);
  if (!prepared.ok()) {
    send(prepared);
    return;
  }
  size_t input_tokens = 0;
  const auto measure = [&](const ResponsesGenerationPlan& candidate,
                           ChatPlanError* error) -> Status {
    if (const auto active = CheckResponsesTokenCountActive(access);
        !active.ok()) {
      *error = {active.code, "input", active.http_status};
      return Status::Fail(active.message);
    }
    const auto& req = candidate.chat.Request(candidate.generation_request);
    std::string prompt;
    std::vector<VisionItem> items;
    auto measured =
        PrepareChatPrompt(req, candidate.chat.constraints, &prompt, &items,
                          &error->param, &error->code, nullptr, true);
    if (!measured) return measured;
    std::vector<uint32_t> tokens;
    ChatPromptBudget budget;
    {
      auto lock = LockUnlessCancelled(tok_mu_, cancelled);
      if (!lock.owns_lock()) {
        const auto active = CheckResponsesTokenCountActive(access);
        *error = {active.code, "input", active.http_status};
        return Status::Fail(active.message);
      }
      measured = EncodeChatPrompt(
          req, config, prompt,
          [&](std::string_view text, std::vector<uint32_t>* out) {
            return tok_->Encode(text, out, {.cancelled = cancelled});
          },
          &tokens, &budget, error);
    }
    if (measured && !items.empty()) {
      const auto& model = model_.Get().cfg;
      vision::MediaPlan media;
      measured = ExpandChatMedia(req, items, model.image_token_id,
                                 model.video_token_id, proc_cfg_,
                                 video_proc_cfg_, vision_device_budget_, config,
                                 &tokens, &media, &budget, error);
    }
    input_tokens = tokens.size();
    return measured;
  };
  ChatPlanError error;
  status = envelope.truncate ? FitResponsesContext(envelope, config, measure,
                                                   &generation, &error)
                             : measure(generation, &error);
  if (const auto active = CheckResponsesTokenCountActive(access);
      !active.ok()) {
    send(active);
    return;
  }
  if (!status) {
    ResponsesInputErrorParam(generation.input, &error.param);
    detail::SendError(fd, error.http_status, status.message(), {}, error.code,
                      error.param);
    return;
  }
  // No Response::Begin, cache mutation, rate reservation or model scheduler.
  detail::SendSimple(fd, 200, "OK",
                     OutputJson(ResponsesTokenCountJson(input_tokens)),
                     "application/json", {access.deadline});
}

}  // namespace q4t::server
