#include "chat_server_internal.h"
#include "chat_model_step.h"

#include "q4t/server/lifetime.h"
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
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!objects_ || !http || !http->Client().Allowed()) {
    detail::SendError(fd, 503, "Authenticated application state unavailable",
                      {}, "object_scope_unavailable");
    return;
  }
  io::Json request;
  auto status = body.empty() ? Status()
                             : io::ParseJson(body, &request, true,
                                             kApplicationJsonLimits);
  if (!status) {
    detail::SendError(fd, 400, "invalid JSON: " + status.message(), {},
                      "invalid_json");
    return;
  }
  const auto& client = http->Client();
  ClientRateLease preparation_rate;
  http->SetBeforeTerminal(
      [](void* value) noexcept {
        static_cast<ClientRateLease*>(value)->Settle();
      },
      &preparation_rate);
  ScopeExit clear_terminal([&] { http->SetBeforeTerminal(nullptr, nullptr); });
  std::shared_ptr<RequestControl> preparation_control;
  ScopeExit remove_control([&] {
    if (preparation_control) requests_.Remove(preparation_control);
  });
  StateAccess access{
      client.Scoped() ? "client:" + std::string(client.Id())
                      : "local:anonymous",
      arrived + std::chrono::milliseconds(request_deadline_ms_), [&, this] {
        return stop_requested_.load() ||
               (preparation_control
                    ? detail::RequestCancelled(preparation_control.get(), fd)
                    : detail::ClientConnectionFailed(fd));
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
  const auto prepare_files = [&](FileExpansion& files) -> ObjectReply {
    preparation_control = requests_.Register(
        detail::NewRequestId("count-auto-"), "", access.deadline, client.Id());
    if (!preparation_control) {
      ObjectReply error;
      error.http_status = 409;
      error.code = "request_id_conflict";
      error.message = "Input preparation identity already active";
      return error;
    }
    ModelPreparation preparation(clients_, client, preparation_rate, 0, 0,
                                 files.PendingOutputBudget());
    uint64_t steps = 0;
    ScopeExit report([&] {
      http->SetPreparationUsage(
          {preparation.InputTokens(), preparation.OutputTokens(), steps});
    });
    while (files.PendingSheets()) {
      FileSheetStep sheet;
      auto result = files.NextSheet(config, &sheet);
      if (!result.ok()) return result;
      const auto before = preparation.InputTokens();
      ScopeExit observe_step([&] {
        if (preparation.InputTokens() > before) ++steps;
      });
      InternalModelStep work{sheet.request,
                             config,
                             preparation,
                             [] { return ObjectReply{}; },
                             {},
                             false,
                             {},
                             {},
                             0,
                             0};
      result = RunInternalModelStep(fd, &work, preparation_control);
      if (!result.ok()) return result;
      result = files.CompleteSheet(sheet, work.content, work.finish_reason,
                                   work.input_tokens, work.output_tokens);
      if (!result.ok()) return result;
    }
    return {};
  };
  auto prepared =
      PrepareResponsesTokenCount(request, config, *objects_, access, &envelope,
                                 &generation, prepare_files);
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
  if (preparation_control && !preparation_control->Finish()) {
    send(CheckResponsesTokenCountActive(access));
    return;
  }
  // No Response::Begin or final-answer generation. For new sheets, actual
  // preparation usage is separately visible in headers and charged once.
  detail::SendSimple(fd, 200, "OK",
                     OutputJson(ResponsesTokenCountJson(input_tokens)),
                     "application/json", {access.deadline});
}

}  // namespace q4t::server
