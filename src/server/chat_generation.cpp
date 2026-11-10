// Existing ChatServer responsibilities; shared state stays in ChatServer.
#include "chat_server_internal.h"
#include "chat_model_step.h"
#include "responses_json.h"

#include <new>
#include <optional>
#include "cuda_memory_backend.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <charconv>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <limits>
#include <span>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>
#include <cuda_runtime.h>
#include "q4t/diagnostic/execution_observer.h"
#include "q4t/mtp/suffix_drafter.h"
#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_choices.h"
#include "q4t/server/chat_file_inputs.h"
#include "q4t/server/chat_storage.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/completion_plan.h"
#include "q4t/server/completion_response.h"
#include "q4t/server/responses_execution.h"
#include "q4t/server/responses_api.h"
#include "q4t/server/responses_reasoning.h"
#include "q4t/server/token_logprobs.h"
#include "q4t/server/media_admission.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/structured_decoder.h"
#include "q4t/server/structured_admission.h"
#include "q4t/server/prefix_cache_policy.h"
#include "q4t/server/request_json.h"
#include "q4t/server/mtp_policy.h"
#include "q4t/server/scheduler_submission.h"
#include "q4t/server/request_diagnostics.h"
#include "q4t/server/request_admission.h"
#include "q4t/server/session_state.h"
#include "q4t/server/thinking_budget.h"
#include "q4t/server/text_stop.h"

namespace q4t::server {
namespace {

// Diagnostic-only memory snapshot (device + host + page cache) for the
// acceptance memory ledger. Never touches the compute path.
void LogMemorySnapshot(const char* phase, const std::string& id) {
  size_t free_bytes = 0, total_bytes = 0;
  if (cudaMemGetInfo(&free_bytes, &total_bytes) != cudaSuccess) return;
  long rss_kb = 0;
  if (FILE* st = std::fopen("/proc/self/status", "r")) {
    char line[256];
    while (std::fgets(line, sizeof line, st)) {
      if (std::strncmp(line, "VmRSS:", 6) == 0) {
        rss_kb = std::atol(line + 6);
        break;
      }
    }
    std::fclose(st);
  }
  long mem_free_kb = 0, cached_kb = 0, mem_avail_kb = 0;
  if (FILE* mi = std::fopen("/proc/meminfo", "r")) {
    char line[256];
    while (std::fgets(line, sizeof line, mi)) {
      if (std::strncmp(line, "MemFree:", 8) == 0)
        mem_free_kb = std::atol(line + 8);
      if (std::strncmp(line, "Cached:", 7) == 0)
        cached_kb = std::atol(line + 7);
      if (std::strncmp(line, "MemAvailable:", 13) == 0)
        mem_avail_kb = std::atol(line + 13);
      if (mem_free_kb != 0 && cached_kb != 0 && mem_avail_kb != 0) break;
    }
    std::fclose(mi);
  }
  std::fprintf(
      stderr,
      "[q4t][mem] phase=%s id=%s dev_used_gb=%.3f dev_total_gb=%.3f "
      "rss_mb=%.1f mem_free_gb=%.3f mem_avail_gb=%.3f cached_gb=%.3f\n",
      phase, id.c_str(), (total_bytes - free_bytes) / 1e9, total_bytes / 1e9,
      rss_kb / 1024.0, mem_free_kb * 1024.0 / 1e9, mem_avail_kb * 1024.0 / 1e9,
      cached_kb * 1024.0 / 1e9);
}

}  // namespace
using detail::FinishHostReadback;
using detail::JsonEscape;
using detail::RequestCancelled;
using detail::SendError;
using detail::SendSimple;
using detail::SseChunk;
using detail::WriteAll;

void ChatServer::HandleChat(int fd, const std::string& body, GenerationApi api,
                            std::shared_ptr<RequestControl> owner,
                            const io::Json* frozen_inputs,
                            const BatchResponseLink* batch_parent) {
  detail::ResponseState response = detail::ResponseState::kUnsent;
  auto* context = detail::HttpResponseContext::Current(fd);
  auto* state = context ? context->State() : &response;
  std::unique_ptr<ResponsesExecution> responses;
  ScopeExit close_responses([&] {
    if (!responses) return;
    try {
      const auto closed = responses->CloseUnfinished();
      if (!closed.ok())
        std::fprintf(stderr,
                     "[responses] state close id=%s code=%s unknown=%d\n",
                     responses->Id().c_str(), closed.code.c_str(),
                     closed.commit_unknown);
    } catch (...) {
      // Cleanup ownership has already transferred through its nonallocating
      // exit guard. Auxiliary state failure never restarts the model.
      std::fprintf(stderr, "[responses] state close unavailable\n");
    }
  });
  ScopeExit clear_response_hooks([&] {
    if (context) {
      context->SetResponsesStream(nullptr, nullptr, nullptr);
      context->SetErrorObserver(nullptr, nullptr);
    }
  });
  try {
    if (api == GenerationApi::kResponse || api == GenerationApi::kCompact) {
      if (!frozen_inputs && api == GenerationApi::kResponse &&
          TryHandleBackgroundResponse(fd, body))
        return;
      if (!objects_ || !context || !context->Client().Allowed()) {
        SendError(fd, 503, "Authenticated application state unavailable", {},
                  "object_scope_unavailable");
        return;
      }
      const auto* frozen_request =
          frozen_inputs ? frozen_inputs->Find("request") : nullptr;
      const bool frozen_background =
          frozen_request && frozen_request->GetBool("background");
      responses = std::make_unique<ResponsesExecution>(
          *objects_, frozen_background ? nullptr : background_.get(),
          !frozen_background, api == GenerationApi::kCompact);
      if (frozen_inputs) {
        if (!owner || api != GenerationApi::kResponse) {
          SendError(fd, 500, "Frozen request owner unavailable", {},
                    "batch_execution_owner_invalid");
          return;
        }
        const auto& client = context->Client();
        StateAccess access{
            client.Scoped() ? "client:" + std::string(client.Id())
                            : "local:anonymous",
            owner->deadline(), [owner] { return owner->Cancelled(); }};
        const auto restored =
            responses->RestoreInputs(*frozen_inputs, PlanningConfig(fd), access,
                                     std::time(nullptr), batch_parent);
        if (!restored.ok()) {
          SendError(fd, restored.http_status, restored.ClientMessage(), {},
                    restored.ClientCode(), restored.value.GetString("param"));
          return;
        }
        if (responses->Envelope().background) {
          if (!background_) {
            SendError(fd, 503, "Background executor unavailable", {},
                      "background_unavailable");
            return;
          }
          ServePreparedBackgroundResponse(fd, *objects_, *background_,
                                           std::move(responses), access);
          return;
        }
      }
      context->SetResponsesStream(&ResponsesExecution::RenderStreamError,
                                  &*responses, responses->Sequence());
      context->SetErrorObserver(&ResponsesExecution::ObserveError, &*responses);
    }
    HandleChatImpl(fd, body, state, api == GenerationApi::kCompletion,
                   responses.get(), std::move(owner), nullptr, frozen_inputs);
  } catch (const std::bad_alloc&) {
    detail::SendExceptionResponse(fd, *state,
                                  detail::ExceptionKind::kHostMemory);
  } catch (const std::exception& error) {
    std::fprintf(stderr, "[q4t] request exception: %s\n", error.what());
    detail::SendExceptionResponse(fd, *state);
  } catch (...) {
    std::fprintf(stderr, "[q4t] unknown request exception\n");
    detail::SendExceptionResponse(fd, *state);
  }
}

ObjectReply ChatServer::RunInternalModelStep(
    int fd, InternalModelStep* step, std::shared_ptr<RequestControl> control) {
  return ExecuteInternalModelStep(
      fd, step, std::move(control),
      [&](detail::ResponseState* state, std::shared_ptr<RequestControl> owner) {
        HandleChatImpl(fd, "", state, false, nullptr, std::move(owner), step);
      });
}

void ChatServer::HandleChatImpl(int fd, const std::string& body,
                                detail::ResponseState* response_state,
                                bool completion, ResponsesExecution* responses,
                                std::shared_ptr<RequestControl> background,
                                InternalModelStep* internal,
                                const io::Json* frozen_inputs) {
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!internal)
    metrics_.requests_total.fetch_add(1, std::memory_order_relaxed);
  const auto t_arrive = std::chrono::steady_clock::now();
  // Any return before generation starts (validation / alloc / encode / prefill
  // failure) counts as a pre-generation error; cleared once a token is
  // produced.
  struct ErrGuard {
    std::atomic<uint64_t>& err;
    bool ok = false;
    bool aborted = false;
    bool enabled = true;
    int exceptions = std::uncaught_exceptions();
    ~ErrGuard() {
      if (enabled && !aborted &&
          (!ok || std::uncaught_exceptions() > exceptions))
        err.fetch_add(1, std::memory_order_relaxed);
    }
  } err_guard{metrics_.requests_error};
  err_guard.enabled = !internal;
  detail::ServeFaultPoint("handler.before_diagnostics");
  RequestDiagnostics diagnostic(stderr, http ? http->TraceId() : "-");
  using Wait = RequestDiagnostics::Wait;
  if (RejectUnhealthy(fd)) return;
  if (!CheckMemoryAdmission(fd, MemoryCheckpoint::kPrepare)) return;
  RequestAdmissionLease chat_lease(active_chats_, admission_limits_.inflight);
  if (!internal && !chat_lease.TryAcquire()) {
    metrics_.text_admission_rejected.fetch_add(1, std::memory_order_relaxed);
    SendError(fd, 503, "text request capacity exhausted", {},
              "capacity_exhausted");
    return;
  }
  bool attempted_slot = false;
  ScopeExit slot_wait_observation([&] {
    if (attempted_slot)
      metrics_.slot_wait_seconds.Observe(diagnostic.Total(Wait::kSlot) /
                                         1000.0);
  });
  // Outlives request/grammar/response buffers; release only after cleanup.
  StructuredRequestLease structured_lease(structured_admission_,
                                          metrics_.structured_active);
  ClientRateLease owned_rate(http ? http->RateDomain()
                                  : ClientRateDomain::kOnline);
  const auto batch_rate = http ? http->BatchRate() : nullptr;
  if (batch_rate && (http->RateDomain() != ClientRateDomain::kBatch ||
                     !batch_rate->PrechargedFor(http->Client()))) {
    SendError(fd, 500, "Native Batch rate ownership changed", {},
              "batch_execution_rate_invalid");
    return;
  }
  auto& rate_lease = internal     ? internal->accounting.Rate()
                     : batch_rate ? *batch_rate
                                  : owned_rate;
  struct TerminalLeases {
    StructuredRequestLease& structured;
    ClientRateLease& rate;
  } terminal_leases{structured_lease, rate_lease};
  if (http && !internal)
    http->SetBeforeTerminal(
        [](void* lease) noexcept {
          auto& leases = *static_cast<TerminalLeases*>(lease);
          leases.rate.Settle();
          leases.structured.BeginHandoff();
        },
        &terminal_leases);
  ScopeExit clear_terminal_hook([&] {
    if (http) http->SetBeforeTerminal(nullptr, nullptr);
  });
  io::Json original_request;
  Status s;
  if (internal)
    original_request = internal->request;
  else if (!responses || !responses->Prepared())
    s = io::ParseJson(
        body, &original_request, true,
        completion ? kCompletionJsonLimits : kApplicationJsonLimits);
  if (!s.ok()) {
    SendError(fd, 400, "invalid JSON: " + s.message(), {}, "invalid_json");
    return;
  }

  auto planning_config = internal ? internal->config : PlanningConfig(fd);
  if (internal) {
    planning_config.client_limits = nullptr;
    planning_config.count_input_only = false;
    planning_config.prewarm_only = false;
  }
  const auto client = http ? http->Client().Id() : std::string_view();
  std::shared_ptr<RequestControl> preparation_control;
  bool owns_preparation_control = false;
  ScopeExit close_preparation_control([&] {
    if (owns_preparation_control) requests_.Remove(preparation_control);
  });
  std::optional<ModelPreparation> preparation;
  std::string preparation_wire;
  ChatRequestPlan request_plan;
  ChatFileInputs chat_files(objects_.get());
  CompletionRequestPlan completion_plan;
  std::vector<PreparedCompletionPrompt> completion_prompts;
  uint64_t completion_prompt_tokens = 0, completion_output_tokens = 0;
  ChatPlanError planning_error;
  if (responses) {
    if (!responses->Prepared()) {
      const auto& identity = http->Client();
      const auto requested_ms =
          original_request.GetInt("request_timeout_ms", request_deadline_ms_);
      // Invalid values are diagnosed by the envelope before acquisition. A
      // valid caller deadline also covers file/prompt I/O, not just decoding.
      const auto preparation_ms = requested_ms >= 1 && requested_ms <= 10800000
                                      ? requested_ms
                                      : request_deadline_ms_;
      StateAccess access{
          identity.Scoped() ? "client:" + std::string(identity.Id())
                            : "local:anonymous",
          t_arrive + std::chrono::milliseconds(preparation_ms), [this, fd] {
            return stop_requested_.load() || detail::ClientConnectionFailed(fd);
          }};
      auto reply = responses->Prepare(original_request, planning_config,
                                      std::move(access), std::time(nullptr));
      if (!reply.ok()) {
        SendError(fd, reply.http_status, reply.ClientMessage(), {},
                  reply.ClientCode(),
                  reply.value.GetString("param"));
        return;
      }
    }
    if (!responses->BindRateDomain(http ? http->RateDomain()
                                        : ClientRateDomain::kOnline)) {
      SendError(fd, 503, "Response accounting origin changed", {},
                "response_rate_domain_changed");
      return;
    }
    planning_config.prewarm_only = responses->Envelope().cache.prewarm;
    if (responses->PendingFileSheets()) {
      const auto& envelope = responses->Envelope();
      const auto& original = envelope.request;
      const auto deadline =
          background
              ? background->deadline()
              : t_arrive + std::chrono::milliseconds(original.GetInt(
                               "request_timeout_ms", request_deadline_ms_));
      preparation_control =
          background
              ? background
              : requests_.Register(
                    original.GetString("request_id", responses->Id()),
                    original.GetString("cancel_token"), deadline, client);
      if (!preparation_control) {
        SendError(fd, 409, "request_id already active", {},
                  "request_id_conflict", "request_id");
        return;
      }
      owns_preparation_control = !background;
      responses->BindCancellation(deadline, [preparation_control, fd] {
        return RequestCancelled(preparation_control.get(), fd);
      });
      const uint64_t auxiliary =
          envelope.moderation.enabled
              ? (envelope.cache.prewarm ? 1 : 2) * kMaxModerationStepTokens
              : 0;
      preparation.emplace(clients_, http->Client(), rate_lease,
                          envelope.cache.prewarm
                              ? 0
                              : envelope.options.GetInt("max_output_tokens"),
                          auxiliary, responses->FileOutputBudget());
      while (responses->PendingFileSheets()) {
        FileSheetStep sheet;
        auto reply = responses->NextFileSheet(planning_config, &sheet);
        if (reply.ok()) {
          InternalModelStep work{
              sheet.request,
              planning_config,
              *preparation,
              [&] {
                return responses->Begun() ? ObjectReply{}
                                          : responses->Begin(&preparation_wire);
              },
              {},
              false,
              {},
              {},
              0,
              0};
          reply = RunInternalModelStep(fd, &work, preparation_control);
          if (reply.ok())
            reply = responses->CompleteFileSheet(
                sheet, work.content, work.finish_reason, work.input_tokens,
                work.output_tokens, planning_config);
        }
        if (!reply.ok()) {
          SendError(fd, reply.http_status, reply.ClientMessage(), {},
                    reply.ClientCode(), reply.value.GetString("param"));
          return;
        }
      }
    }
    if (responses->Envelope().truncate) {
      const auto context_control =
          preparation_control ? preparation_control : background;
      const auto context_deadline =
          context_control
              ? context_control->deadline()
              : t_arrive +
                    std::chrono::milliseconds(
                        responses->Generation().generation_request.GetInt(
                            "request_timeout_ms", request_deadline_ms_));
      const auto context_cancelled = [this, fd, context_control,
                                      context_deadline] {
        return stop_requested_.load() ||
               std::chrono::steady_clock::now() >= context_deadline ||
               (context_control ? context_control->Cancelled()
                                : detail::ClientConnectionFailed(fd));
      };
      responses->BindCancellation(context_deadline, context_cancelled);
      auto reply = responses->FitContext(
          planning_config,
          [&](const ResponsesGenerationPlan& candidate,
              ChatPlanError* error) -> Status {
            const auto& request =
                candidate.chat.Request(candidate.generation_request);
            std::string text;
            std::vector<VisionItem> media;
            auto measured =
                PrepareChatPrompt(request, candidate.chat.constraints, &text,
                                  &media, &error->param, &error->code);
            if (!measured) return measured;
            std::vector<uint32_t> tokens;
            ChatPromptBudget budget;
            // Context truncation is not a way to evade a client's token
            // quota. Enforce that quota on the final selected input below.
            auto context_config = planning_config;
            context_config.client_limits = nullptr;
            {
              auto lock =
                  diagnostic.Lock(tok_mu_, Wait::kTokenizer, context_cancelled);
              if (!lock.owns_lock()) {
                const bool expired =
                    std::chrono::steady_clock::now() >= context_deadline;
                *error = {
                    expired ? "request_deadline_exceeded" : "request_cancelled",
                    "input", expired ? 408 : 499};
                return Status::Fail("Context tokenizer wait cancelled");
              }
              measured = EncodeChatPrompt(
                  request, context_config, text,
                  [&](std::string_view value, std::vector<uint32_t>* out) {
                    return tok_->Encode(value, out,
                                        {.cancelled = context_cancelled});
                  },
                  &tokens, &budget, error);
            }
            if (measured && !media.empty()) {
              vision::MediaPlan expanded;
              const auto& model = model_.Get().cfg;
              measured = ExpandChatMedia(
                  request, media, model.image_token_id, model.video_token_id,
                  proc_cfg_, video_proc_cfg_, vision_device_budget_,
                  context_config, &tokens, &expanded, &budget, error);
            }
            if (context_cancelled()) {
              const bool expired =
                  std::chrono::steady_clock::now() >= context_deadline;
              *error = {
                  expired ? "request_deadline_exceeded" : "request_cancelled",
                  "input", expired ? 408 : 499};
              return Status::Fail("Context preparation cancelled");
            }
            return measured;
          });
      if (!reply.ok()) {
        SendError(fd, reply.http_status, reply.ClientMessage(), {},
                  reply.ClientCode(), reply.value.GetString("param"));
        return;
      }
    }
    original_request = responses->Generation().generation_request;
    request_plan = responses->Generation().chat;
  } else if (completion) {
    s = PlanCompletionRequest(original_request, planning_config,
                               &completion_plan, &planning_error);
    if (s) {
      request_plan.normalized_request = completion_plan.generation_request;
      request_plan.sampling = completion_plan.sampling;
      request_plan.stops = completion_plan.stops;
    }
  } else {
    auto validation_config = planning_config;
    validation_config.preparing_file_inputs = !internal;
    s = PlanChatRequest(original_request, validation_config, &request_plan,
                        &planning_error);
    const auto* messages = original_request.GetArray("messages");
    if (s && !internal && messages && HasFileInputs(*messages)) {
      if (original_request.GetBool("store") &&
          (!objects_ || !http || !http->Client().Allowed())) {
        SendError(fd, 503, "Authenticated application state unavailable", {},
                  "object_scope_unavailable", "store");
        return;
      }
      const auto deadline =
          background
              ? background->deadline()
              : t_arrive + std::chrono::milliseconds(original_request.GetInt(
                               "request_timeout_ms", request_deadline_ms_));
      preparation_control =
          background
              ? background
              : requests_.Register(
                    original_request.GetString(
                        "request_id", detail::NewRequestId("chatcmpl-auto-")),
                    original_request.GetString("cancel_token"), deadline,
                    client);
      if (!preparation_control) {
        SendError(fd, 409, "request_id already active", {},
                  "request_id_conflict", "request_id");
        return;
      }
      owns_preparation_control = !background;
      const BinaryAccess access{
          http && http->Client().Scoped()
              ? "client:" + std::string(http->Client().Id())
              : "local:anonymous",
          deadline, [preparation_control, fd] {
            return RequestCancelled(preparation_control.get(), fd);
          }};
      const auto* bindings = frozen_inputs
                                 ? frozen_inputs->Find("input_snapshots")
                                 : nullptr;
      auto prepared = chat_files.Prepare(
          original_request, planning_config, access, &request_plan,
          bindings ? *bindings : io::Json(), frozen_inputs != nullptr);
      if (!prepared.ok()) {
        SendError(fd, prepared.http_status, prepared.ClientMessage(), {},
                  prepared.ClientCode(), prepared.value.GetString("param"));
        return;
      }
    }
  }
  if (!s) {
    SendError(fd, planning_error.http_status, s.message(), {},
              planning_error.code, planning_error.param);
    return;
  }
  const auto& req = request_plan.Request(original_request);
  ChatStorageOptions chat_storage;
  if (!completion && !responses) {
    s = ParseChatStorageOptions(req, &chat_storage, &planning_error.param);
    if (!s) {
      SendError(fd, 400, s.message(), {}, "invalid_request",
                planning_error.param);
      return;
    }
    if (chat_storage.store &&
        (!objects_ || !http || !http->Client().Allowed())) {
      SendError(fd, 503, "Authenticated application state unavailable", {},
                "object_scope_unavailable", "store");
      return;
    }
  }
  const auto& constraints = request_plan.constraints;
  const auto& sampling = request_plan.sampling;
  const bool plain_greedy = sampling.PlainGreedy();
  const auto& cache_options = responses ? responses->Envelope().cache
                                         : request_plan.cache;
  const bool api_cache = !completion && (responses || cache_options.requested);
  const bool cache_prewarm = responses && cache_options.prewarm;
  const std::string api_cache_key =
      "api-v1:" + trace::HexDigest(trace::Sha256(
                      {reinterpret_cast<const uint8_t*>(cache_options.key.data()),
                       cache_options.key.size()}));
  const std::string request_prefix_scope = ClientScopedKey(
      client, api_cache ? api_cache_key : req.GetString("prefix_cache_scope"));
  const int request_prefix_write_tokens =
      static_cast<int>(req.GetInt("prefix_cache_write_tokens", 0));
  std::vector<std::unique_ptr<SessionLease>> shared_writes;
  SessionLease request_session;
  request_session.key = ClientScopedKey(client, req.GetString("session_id"));
  if (!request_session.key.empty()) {
    if (!session_cache_.Acquire(request_session.key)) {
      SendError(fd, 409, "session is busy or lease capacity exhausted", {},
                "session_busy", "session_id");
      return;
    }
    request_session.cache = &session_cache_;
  }

  const io::Json* external_id = req.Find("request_id");
  const std::string key = req.GetString("cancel_token");
  std::string request_id = req.GetString("request_id");
  // Shape, credentials and exact integer spelling were checked before leases.
  if (!external_id)
    request_id = preparation_control ? preparation_control->id()
                 : responses
                     ? responses->Id()
                     : detail::NewRequestId(completion ? "cmpl-auto-"
                                                       : "chatcmpl-auto-");
  auto deadline = t_arrive + std::chrono::milliseconds(request_deadline_ms_);
  if (req.Has("request_timeout_ms")) {
    deadline =
        t_arrive + std::chrono::milliseconds(req.GetInt("request_timeout_ms"));
  }
  detail::ServeFaultPoint("handler.before_register");
  if (background) deadline = background->deadline();
  auto control = preparation_control ? preparation_control
                 : background
                     ? background
                     : requests_.Register(request_id, key, deadline, client);
  if (!control) {
    SendError(fd, 409, "request_id already active", {}, "request_id_conflict",
              "request_id");
    return;
  }
  struct Registration {
    RequestRegistry* registry;
    std::shared_ptr<RequestControl> control;
    RequestDiagnostics& diagnostic;
    ~Registration() {
      if (control->Cancelled())
        diagnostic.Outcome(control->Failed() ? "error" : "cancelled",
                           control->Reason());
      if (registry) registry->Remove(control);
    }
  } registration{background || preparation_control ? nullptr : &requests_,
                 control, diagnostic};
  OnException request_failure(
      [&] { control->Cancel(RequestControl::State::kFailure); });
  auto progress =
      progress_.Acquire(std::chrono::duration_cast<std::chrono::nanoseconds>(
                            deadline.time_since_epoch())
                            .count());
  control->BindProgress(progress.Handle());
  diagnostic.BindProgress(progress.Handle());
  ProgressContext progress_context(progress.Handle());
  diagnostic.Bind(request_id);
  if (http)
    std::fprintf(stderr, "[q4t][http] trace_id=%s completion_id=%s\n",
                 http->TraceId().c_str(), request_id.c_str());
  detail::ServeFaultPoint("handler.after_register");
  const auto cancelled = [&] { return RequestCancelled(control.get(), fd); };
  StateAccess chat_state_access{
      http && http->Client().Scoped()
          ? "client:" + std::string(http->Client().Id())
          : "local:anonymous",
      deadline, cancelled};
  if (chat_storage.store) {
    RequestDiagnostics::Scope waiting(diagnostic, Wait::kStorage);
    const auto existing =
        StoredChats(*objects_).Retrieve(request_id, chat_state_access);
    if (existing.ok() || existing.http_status != 404) {
      SendError(fd, existing.ok() ? 409 : existing.http_status,
                existing.ok() ? "Stored completion ID already exists"
                              : existing.message,
                {}, existing.ok() ? "object_revision_conflict" : existing.code,
                "store");
      return;
    }
  }
  if (responses)
    responses->BindCancellation(deadline, [control, fd] {
      return RequestCancelled(control.get(), fd);
    });
  const auto send_cancel_error = [&](const char* message) {
    SendError(
        fd, 409, message,
        {std::chrono::steady_clock::now() + std::chrono::milliseconds(250)},
        control->Reason());
  };
  bool abort_counted = false;
  const auto count_abort = [&] {
    err_guard.ok = true;  // A cancellation is not also a validation error.
    err_guard.aborted = true;
    if (!abort_counted && !internal) {
      metrics_.requests_aborted.fetch_add(1, std::memory_order_relaxed);
      metrics_.abort_reasons[AbortReasonIndex(control->Reason())].fetch_add(
          1, std::memory_order_relaxed);
      abort_counted = true;
      std::fprintf(stderr, "[q4t] request cancelled id=%s reason=%s\n",
                   request_id.c_str(), control->Reason());
    }
    err_guard.ok = true;
  };

  const int request_output_limit = completion
                                       ? completion_plan.max_tokens
                                       : ChatOutputLimit(req, planning_config);

  // stream flag.
  const bool stream = req.GetBool("stream", false);
  const io::Json* stream_options = req.Find("stream_options");
  const bool include_usage =
      stream_options && stream_options->GetBool("include_usage", false);
  const std::string id = request_id;
  const std::string model_field = req.GetString("model", model_name_);
  const int64_t created = static_cast<int64_t>(std::time(nullptr));
  ScopeExit stop_heartbeat([&] {
    if (http) http->StopHeartbeats();
  });

  std::string prompt;
  std::vector<VisionItem> items;
  ChatPromptLayout prompt_layout;
  if (!completion)
    s = PrepareChatPrompt(req, constraints, &prompt, &items,
                          &planning_error.param, &planning_error.code,
                          api_cache ? &prompt_layout : nullptr);
  if (!s) {
    SendError(fd, 400, s.message(), {}, planning_error.code,
              planning_error.param);
    return;
  }
  std::vector<std::uint32_t> prompt_u32;
  ChatPromptBudget prompt_budget;
  vision::MediaPlan media_plan;
  bool prompt_encoded = false;
  const auto encode_prompt = [&] {
    diagnostic.Stage("encode");
    Status encoded;
    {
      auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer, cancelled);
      if (!lock.owns_lock())
        return Status::Fail("request cancelled waiting for prompt tokenizer");
      encoded = EncodeChatPrompt(
          req, planning_config, prompt,
          [&](std::string_view text, std::vector<uint32_t>* out) {
            return tok_->Encode(text, out, {.cancelled = cancelled});
          },
          &prompt_u32, &prompt_budget, &planning_error);
    }
    if (encoded && !items.empty()) {
      const auto& config = model_.Get().cfg;
      encoded = ExpandChatMedia(
          req, items, config.image_token_id, config.video_token_id, proc_cfg_,
          video_proc_cfg_, vision_device_budget_, planning_config, &prompt_u32,
          &media_plan, &prompt_budget, &planning_error);
    }
    prompt_encoded = encoded.ok();
    return encoded;
  };
  // Count expanded visual tokens before rate reservation, SSE or grammar.
  // Preflight uses the same expansion and budget, without reserving quota.
  {
    if (completion) {
      auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer, cancelled);
      if (!lock.owns_lock()) {
        count_abort();
        send_cancel_error("request cancelled waiting for completion tokenizer");
        return;
      }
      s = PrepareCompletionPrompts(
          completion_plan, planning_config,
          [&](std::string_view text, std::vector<uint32_t>* out) {
            return tok_->Encode(text, out, {.cancelled = cancelled});
          },
          [&](const std::vector<uint32_t>& ids, std::string* out) {
            return tok_->Decode(ids, false, out);
          },
          &completion_prompts, &completion_prompt_tokens,
          &completion_output_tokens, &planning_error);
      for (size_t i = 0; s && i < completion_prompts.size(); ++i) {
        auto& prepared = completion_prompts[i];
        if (completion_plan.ScoresPrompt()) {
          const std::vector<uint32_t> echoed(
              prepared.tokens.begin() + prepared.echo_begin,
              prepared.tokens.begin() + prepared.echo_end);
          std::vector<size_t> offsets;
          s = tok_->EchoByteOffsets(prepared.echo_text, echoed,
                                     !completion_plan.prompts[i].tokenized,
                                     &offsets, {.cancelled = cancelled});
          if (s) {
            const std::string original = prepared.echo_text;
            s = CompletionEchoOffsets(original, offsets, &prepared.echo_text,
                                        &prepared.echo_offsets);
          }
          if (!s && !cancelled())
            planning_error = {"completion_echo_alignment_failed", "prompt",
                              500};
        } else {
          prepared.echo_text = detail::NormalizeUtf8(prepared.echo_text);
        }
      }
      prompt_encoded = s.ok();
    } else {
      s = encode_prompt();
    }
    if (!s) {
      if (cancelled()) {
        count_abort();
        send_cancel_error("request cancelled during encoding");
      } else {
        SendError(fd, planning_error.http_status, s.message(), {},
                  planning_error.code,
                  planning_error.param);
      }
      return;
    }
    if (cancelled()) {
      count_abort();
      send_cancel_error("request cancelled during media planning");
      return;
    }
  }
  // Shared actual template/token/media measurement for compact output. It
  // performs no generation and cannot conceal a malformed or oversized window.
  const auto measure_context = [&](const ResponsesGenerationPlan& candidate,
                                   size_t* count, ChatPlanError* error) {
    const auto& candidate_req =
        candidate.chat.Request(candidate.generation_request);
    std::string text;
    std::vector<VisionItem> media;
    auto status = PrepareChatPrompt(candidate_req, candidate.chat.constraints,
                                    &text, &media, &error->param, &error->code);
    std::vector<uint32_t> tokens;
    ChatPromptBudget budget;
    auto config = planning_config;
    config.client_limits = nullptr;
    if (status) {
      auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer, cancelled);
      if (!lock.owns_lock()) {
        *error = {"request_cancelled", "input", 499};
        return Status::Fail("Compaction measurement cancelled");
      }
      status = EncodeChatPrompt(
          candidate_req, config, text,
          [&](std::string_view value, std::vector<uint32_t>* out) {
            return tok_->Encode(value, out, {.cancelled = cancelled});
          },
          &tokens, &budget, error);
    }
    if (status && !media.empty()) {
      vision::MediaPlan expanded;
      const auto& model = model_.Get().cfg;
      status = ExpandChatMedia(candidate_req, media, model.image_token_id,
                               model.video_token_id, proc_cfg_, video_proc_cfg_,
                               vision_device_budget_, config, &tokens,
                               &expanded, &budget, error);
    }
    if (status) *count = tokens.size();
    return status;
  };
  const auto prepare_step = [&](const ChatRequestPlan& step,
                                const io::Json& original,
                                bool auxiliary = false) {
    auto step_config = planning_config;
    if (auxiliary) {
      step_config.client_limits = nullptr;
      step_config.prewarm_only = false;
    }
    const auto& step_request = step.Request(original);
    items.clear();
    media_plan = {};
    auto status =
        PrepareChatPrompt(step_request, step.constraints, &prompt, &items,
                          &planning_error.param, &planning_error.code,
                          api_cache ? &prompt_layout : nullptr);
    if (status) {
      auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer, cancelled);
      if (!lock.owns_lock())
        return Status::Fail("Internal step cancelled waiting for tokenizer");
      status = EncodeChatPrompt(
          step_request, step_config, prompt,
          [&](std::string_view text, std::vector<uint32_t>* out) {
            return tok_->Encode(text, out, {.cancelled = cancelled});
          },
          &prompt_u32, &prompt_budget, &planning_error);
    }
    if (status && !items.empty()) {
      const auto& model = model_.Get().cfg;
      status = ExpandChatMedia(step_request, items, model.image_token_id,
                               model.video_token_id, proc_cfg_, video_proc_cfg_,
                               vision_device_budget_, step_config, &prompt_u32,
                               &media_plan, &prompt_budget, &planning_error);
    }
    prompt_encoded = status.ok();
    return status;
  };
  if (responses && !cache_prewarm) {
    const auto selected =
        responses->SelectCompaction(prompt_u32.size(), planning_config);
    if (!selected.ok()) {
      SendError(fd, selected.http_status, selected.ClientMessage(), {},
                selected.ClientCode(), selected.value.GetString("param"));
      return;
    }
  }
  const bool compact_selected = responses && responses->CompactionSelected();
  const bool compact_step_needed =
      compact_selected && responses->Compaction().compacted_items != 0;
  const bool standalone_compact =
      responses && responses->StandaloneCompaction();
  const bool pro_selected = responses && !cache_prewarm &&
                            !standalone_compact &&
                            responses->Envelope().reasoning_mode == "pro";
  ResponsesSummaryPlan pro_plan;
  std::string pro_notes;
  const auto moderation_options =
      responses ? responses->Envelope().moderation : ModerationOptions{};
  const bool moderate_output = moderation_options.enabled && !cache_prewarm;
  ModerationPlan input_moderation, output_moderation;
  ModerationUsage moderation_usage;
  auto moderation_results = responses_detail::Object(
      {{"input", ModerationError("moderation_not_executed",
                                 "Input classification has not executed")},
       {"output", ModerationError("moderation_not_executed",
                                  "Output classification has not executed")}});
  bool input_review = false;
  if (moderation_options.enabled) {
    ModerationContent content;
    s = CollectModerationInput(*req.GetArray("messages"), &content,
                               &planning_error);
    if (s)
      s = PlanModeration(moderation_options, content, planning_config,
                         kMaxModerationStepTokens, &input_moderation,
                         &planning_error);
    if (s)
      s = prepare_step(input_moderation.chat, input_moderation.request, true);
    if (!s) {
      if (cancelled()) {
        count_abort();
        send_cancel_error("Input moderation preparation cancelled");
        return;
      }
      responses_detail::Set(&moderation_results, "input",
                            ModerationError("moderation_preparation_failed",
                                            s.message().substr(0, 1024)));
      if (moderation_options.input == ModerationMode::kBlock) {
        SendError(fd, 503, "Required input classification unavailable", {},
                  "moderation_unavailable", "moderation");
        return;
      }
      // The rejected auxiliary preparation may have changed prompt buffers.
      // Restore the actual primary before admission; never execute its prompt
      // under the classifier plan or charge the wrong input.
      s = prepare_step(request_plan, original_request);
      if (!s) {
        SendError(fd, planning_error.http_status, s.message(), {},
                  planning_error.code, planning_error.param);
        return;
      }
    } else {
      input_review = true;
    }
  }
  if (compact_step_needed && !input_review) {
    s = prepare_step(responses->Compaction().chat,
                     responses->Compaction().request);
    if (!s) {
      if (cancelled()) {
        count_abort();
        send_cancel_error("Compaction preparation cancelled");
      } else {
        SendError(fd, planning_error.http_status, s.message(), {},
                  planning_error.code, "context_management");
      }
      return;
    }
  } else if (pro_selected && !compact_step_needed) {
    s = PlanResponsesDeliberation(responses->Generation(), request_output_limit,
                                  0, planning_config, &pro_plan,
                                  &planning_error);
    if (s && !input_review) s = prepare_step(pro_plan.chat, pro_plan.request);
    if (!s) {
      if (cancelled()) {
        count_abort();
        send_cancel_error("Pro deliberation preparation cancelled");
      } else {
        SendError(fd, planning_error.http_status, s.message(), {},
                  planning_error.code, "reasoning.mode");
      }
      return;
    }
  }
  const uint64_t prepared_input = preparation ? preparation->InputTokens() : 0;
  const uint64_t prepared_output =
      preparation ? preparation->OutputTokens() : 0;
  if (internal) {
    auto reply = internal->accounting.ReservePrompt(prompt_u32.size());
    if (reply.ok()) reply = internal->before_model();
    if (!reply.ok()) {
      SendError(fd, reply.http_status, reply.ClientMessage(), {},
                reply.ClientCode(), reply.value.GetString("param"));
      return;
    }
  } else if (planning_config.client_limits) {
    if (prepared_input + prompt_u32.size() >
        planning_config.client_limits->max_prompt_tokens) {
      SendError(fd, 400, "Initial step exceeds client input limit", {},
                "client_prompt_limit_exceeded", "moderation");
      return;
    }
    const auto result =
        preparation && preparation->Reserved()
            ? rate_lease.ExtendPrompt(clients_, http->Client(),
                                      prompt_u32.size())
            : rate_lease.Reserve(
                  clients_, http->Client(),
                  completion ? completion_prompt_tokens
                  : standalone_compact && !compact_step_needed
                      ? 0
                      : prompt_u32.size(),
                  completion ? completion_output_tokens
                  : standalone_compact && !compact_step_needed ? 0
                  : cache_prewarm                              ? 0
                  : responses ? request_output_limit
                              : prompt_budget.total_output,
                  ClientClock::now(),
                  (input_review + moderate_output) * kMaxModerationStepTokens);
    if (result != ClientRateResult::kAllowed) {
      const char* code =
          result == ClientRateResult::kRpm   ? "client_rpm_exhausted"
          : result == ClientRateResult::kTpm ? "client_tpm_exhausted"
          : result == ClientRateResult::kFrozen ? "service_draining"
                                                : "client_rate_invalid";
      const int status = result == ClientRateResult::kInvalid ? 500
                         : result == ClientRateResult::kFrozen ? 503
                                                              : 429;
      SendError(fd, status,
                "client minute quota unavailable", {}, code);
      return;
    }
  }

  const bool collect_chat = !stream || chat_storage.store;
  const bool batch_usage =
      http && http->RateDomain() == ClientRateDomain::kBatch;
  ChatChoices choices(sampling.choices, !collect_chat, batch_usage);
  CompletionChoices completion_choices(
      static_cast<int>(completion_plan.prompts.size()), completion_plan.n,
      completion_plan.best_of, stream);
  ResponsesSummaryPlan summary_plan;
  ResponsesUsage responses_usage;
  std::optional<PromptCacheDiagnostics::Observation> cache_observation;
  size_t responses_completed_calls = 0;
  const auto completed_tokens = [&] {
    if (responses)
      return static_cast<uint64_t>(responses_usage.output_tokens +
                                   moderation_usage.output_tokens);
    return completion ? completion_choices.CompletionTokens()
                      : choices.CompletionTokens();
  };
  const auto observe_completion = [&](uint64_t count) {
    if (internal)
      internal->accounting.ObserveOutput(count);
    else
      rate_lease.Completion(prepared_output + count);
  };
  bool headers_started = false;
  uint64_t admitted_completion_prompt_tokens = 0;
  bool first_delta_sent = false;
  uint64_t entropy = 0;
  if (!sampling.seed && !plain_greedy) {
    const auto random = detail::NewRequestId("");
    const auto parsed =
        std::from_chars(random.data(), random.data() + 16, entropy, 16);
    if (parsed.ec != std::errc())
      throw std::runtime_error("invalid OS random seed");
  }
  bool client_disconnected = false;
  const auto write_stream = [&](const std::string& data) {
    if (client_disconnected) return false;
    RequestDiagnostics::Scope wait(diagnostic, Wait::kWrite);
    if (!cancelled() && WriteAll(fd, data, {.control = control.get()}))
      return true;
    client_disconnected = true;
    control->Cancel(RequestControl::State::kDisconnect);
    count_abort();
    return false;
  };

  enum class ResponseStep {
    kInputReview,
    kCompaction,
    kPro,
    kPrimary,
    kSummary,
    kOutputReview,
    kFinish
  };
  const auto first_business_step = compact_step_needed
                                       ? ResponseStep::kCompaction
                                   : pro_selected ? ResponseStep::kPro
                                                  : ResponseStep::kPrimary;
  auto response_step =
      input_review ? ResponseStep::kInputReview : first_business_step;
  bool response_incomplete = false, response_filtered = false;
  bool output_hold_started = false;
  bool compact_noop_pending = compact_selected && !compact_step_needed;
  const auto begin_response = [&](std::string* wire) {
    ObjectReply result;
    *wire = std::exchange(preparation_wire, {});
    if (!responses->Begun()) result = responses->Begin(wire);
    if (result.ok() && moderate_output &&
        moderation_options.output == ModerationMode::kBlock &&
        !output_hold_started) {
      result = responses->HoldUnreviewedOutput();
      if (result.ok()) output_hold_started = true;
    }
    return result;
  };
  const auto finish_response = [&] {
    std::string wire, terminal;
    auto result = begin_response(&wire);
    if (result.ok() && moderation_options.enabled) {
      s = responses->SetModerationResults(moderation_results, moderation_usage);
      if (!s) {
        SendError(fd, 503, s.message(), {}, "moderation_metadata_failed");
        return false;
      }
    }
    if (result.ok()) {
      diagnostic.Stage("storage");
      RequestDiagnostics::Scope waiting(diagnostic, Wait::kStorage);
      auto total_usage = responses_usage;
      total_usage.input_tokens += prepared_input;
      total_usage.output_tokens += prepared_output;
      result = responses->Finish(response_incomplete, responses_completed_calls,
                                 total_usage, std::time(nullptr), &terminal,
                                 response_filtered);
    }
    if (!result.ok()) {
      err_guard.ok = false;
      SendError(fd, result.http_status, result.ClientMessage(), {},
                result.ClientCode());
      return false;
    }
    if (cache_observation && !response_filtered)
      prompt_cache_diagnostics_.Remember(client, responses->Id(),
                                         std::move(*cache_observation));
    if (stream) {
      if (!headers_started) {
        if (!detail::BeginStreamingResponse(fd, response_state,
                                            {.control = control.get()})) {
          count_abort();
          return false;
        }
        headers_started = true;
      }
      if (!wire.empty() && !write_stream(wire)) return false;
      if (http) http->BeforeTerminal();
      *response_state = detail::ResponseState::kTerminal;
      if (!write_stream(terminal)) return false;
    } else if (!SendSimple(fd, 200, "OK", responses->ResultJson(),
                           "application/json", {.control = control.get()})) {
      control->Cancel(RequestControl::State::kDisconnect);
      count_abort();
      return false;
    }
    *response_state = detail::ResponseState::kComplete;
    if (control->Cancelled() || !control->Finish()) {
      count_abort();
      return false;
    }
    err_guard.ok = true;
    const auto elapsed = std::chrono::duration<double>(
                             std::chrono::steady_clock::now() - t_arrive)
                             .count();
    metrics_.response_seconds.Observe(elapsed);
    metrics_.e2e_seconds.Observe(elapsed);
    request_session.success = !response_filtered;
    for (auto& lease : shared_writes) lease->success = !response_filtered;
    diagnostic.Outcome("success",
                       response_filtered ? "content_filter" : "completed");
    metrics_.requests_success.fetch_add(1, std::memory_order_relaxed);
    return true;
  };
  const auto finish_review = [&](bool input, io::Json report) {
    const auto mode =
        input ? moderation_options.input : moderation_options.output;
    const auto decision = DecideModeration(mode, report);
    responses_detail::Set(&moderation_results, input ? "input" : "output",
                          std::move(report));
    if (responses->Begun()) {
      s = responses->SetModerationResults(moderation_results, moderation_usage);
      if (!s) {
        SendError(fd, 503, s.message(), {}, "moderation_metadata_failed");
        return false;
      }
    }
    if (decision == ModerationDecision::kUnavailable) {
      responses->DiscardUnreviewedOutput();
      SendError(fd, 503, "Required classification unavailable", {},
                "moderation_unavailable", "moderation");
      return false;
    }
    if (decision == ModerationDecision::kBlocked) {
      responses->DiscardUnreviewedOutput();
      response_incomplete = response_filtered = true;
      response_step = ResponseStep::kFinish;
      return true;
    }
    if (!input && responses->OutputHeld()) {
      std::string wire;
      const auto published = responses->PublishReviewedOutput(
          *moderation_results.Find("output"), &wire);
      if (!published.ok()) {
        SendError(fd, published.http_status, published.ClientMessage(), {},
                  published.ClientCode());
        return false;
      }
      if (stream && !write_stream(wire)) return false;
    }
    response_step = input ? first_business_step : ResponseStep::kFinish;
    return true;
  };
  ResponsesGenerationPlan continuation_generation;
  // At most six model steps and one non-model terminal. No automatic retries
  // of a generated answer, classification, compaction, or tool call.
  for (int choice_index = 0; choice_index < (responses ? 7 : sampling.choices);
       ++choice_index) {
    if (responses && response_step == ResponseStep::kFinish) {
      finish_response();
      return;
    }
    if (compact_noop_pending && response_step != ResponseStep::kInputReview) {
      std::string wire, part;
      auto result = begin_response(&wire);
      if (result.ok()) result = responses->StartCompaction(&part);
      wire += part;
      part.clear();
      if (result.ok())
        result = responses->CompleteCompaction("", 0, planning_config,
                                               measure_context, &part);
      wire += part;
      if (!result.ok()) {
        SendError(fd, result.http_status, result.ClientMessage(), {},
                  result.ClientCode(), result.value.GetString("param"));
        return;
      }
      compact_noop_pending = false;
      if (standalone_compact) {
        rate_lease.StartPrompt(0);
        finish_response();
        return;
      }
      if (stream) {
        if ((!headers_started &&
             !detail::BeginStreamingResponse(fd, response_state,
                                             {.control = control.get()})) ||
            !write_stream(wire))
          return;
        headers_started = true;
        if (http) http->StartHeartbeats(control);
      }
    }
    const bool compaction_step =
        responses && response_step == ResponseStep::kCompaction;
    const bool pro_step = responses && response_step == ResponseStep::kPro;
    const bool summary_step =
        responses && response_step == ResponseStep::kSummary;
    const bool input_moderation_step =
        responses && response_step == ResponseStep::kInputReview;
    const bool moderation_step =
        input_moderation_step ||
        (responses && response_step == ResponseStep::kOutputReview);
    const auto review_failed = [&](const char* code,
                                   const std::string& message) {
      return finish_review(input_moderation_step,
                           ModerationError(code, message.substr(0, 1024)));
    };
    const auto& moderation_plan =
        input_moderation_step ? input_moderation : output_moderation;
    const bool internal_step = internal || compaction_step || pro_step ||
                               summary_step || moderation_step;
    const bool compacted_primary = compact_step_needed && !internal_step;
    const bool continued_primary =
        (compact_step_needed || pro_selected) && !internal_step;
    const auto& step_plan = moderation_step     ? moderation_plan.chat
                            : compaction_step   ? responses->Compaction().chat
                            : pro_step          ? pro_plan.chat
                            : summary_step      ? summary_plan.chat
                            : continued_primary ? continuation_generation.chat
                                                : request_plan;
    const auto& req = step_plan.Request(
        moderation_step     ? moderation_plan.request
        : compaction_step   ? responses->Compaction().request
        : pro_step          ? pro_plan.request
        : summary_step      ? summary_plan.request
        : continued_primary ? continuation_generation.generation_request
                            : original_request);
    const auto& constraints = step_plan.constraints;
    const auto& sampling = step_plan.sampling;
    const bool plain_greedy = sampling.PlainGreedy();
    const bool visible_logprobs =
        !internal_step && sampling.logprobs &&
        (!completion || completion_plan.logprobs >= 0);
    const bool prediction_enabled = step_plan.prediction.enabled;
    const bool prewarm = cache_prewarm && !internal_step;
    const int max_tokens = prewarm           ? 0
                           : moderation_step ? kMaxModerationStepTokens
                           : responses ? ChatOutputLimit(req, planning_config)
                                       : request_output_limit;
    std::string prefix_scope = internal_step ? "" : request_prefix_scope;
    SessionLease summary_session;
    auto& session = internal_step ? summary_session : request_session;
    if (responses && choice_index > 0) {
      err_guard.ok = false;
      diagnostic.Stage("encode");
      const char* param = moderation_step ? "moderation"
                          : summary_step  ? "reasoning.summary"
                          : pro_selected  ? "reasoning.mode"
                                          : "context_management";
      s = prepare_step(step_plan, req, moderation_step);
      if (!s) {
        if (cancelled()) {
          count_abort();
          send_cancel_error("Internal step preparation cancelled");
        } else if (moderation_step) {
          if (!finish_review(input_moderation_step,
                             ModerationError("moderation_preparation_failed",
                                             s.message().substr(0, 1024))))
            return;
          continue;
        } else {
          SendError(fd, planning_error.http_status, s.message(), {},
                    planning_error.code, param);
        }
        return;
      }
      if (planning_config.client_limits) {
        const uint64_t total_prompt =
            prepared_input + responses_usage.input_tokens +
            moderation_usage.input_tokens + prompt_u32.size();
        if (total_prompt > planning_config.client_limits->max_prompt_tokens) {
          if (moderation_step) {
            if (!review_failed("client_prompt_limit_exceeded",
                               "Classification cumulative input limit"))
              return;
            continue;
          }
          SendError(fd, 400,
                    "Internal step exceeds cumulative client input limit", {},
                    "client_prompt_limit_exceeded", param);
          return;
        }
        const auto extended = rate_lease.ExtendPrompt(clients_, http->Client(),
                                                      prompt_u32.size());
        if (extended != ClientRateResult::kAllowed) {
          const bool tpm = extended == ClientRateResult::kTpm;
          if (moderation_step) {
            if (!review_failed(
                    tpm ? "client_tpm_exhausted" : "step_admission_unavailable",
                    "Classification input reservation unavailable"))
              return;
            continue;
          }
          SendError(fd, tpm ? 429 : 503,
                    "Internal step input reservation unavailable", {},
                    tpm ? "client_tpm_exhausted" : "step_admission_unavailable",
                    param);
          return;
        }
      }
    }
    PreparedCompletionPrompt* completion_prompt = nullptr;
    if (completion) {
      completion_prompt =
          &completion_prompts[choice_index / completion_plan.best_of];
      prompt_u32 = completion_prompt->tokens;
      prompt_budget = completion_prompt->budget;
      prompt = completion_prompt->echo_text;
    }
    const bool score_prompt = completion && completion_plan.ScoresPrompt() &&
                              completion_prompt->echo_end >
                                  completion_prompt->echo_begin;
    CompletionProbabilityRows prompt_probabilities;
    double completion_score = 0;
    uint64_t completion_scored_tokens = 0;
    err_guard.ok = false;
    if (RejectUnhealthy(fd)) return;
    if (!CheckMemoryAdmission(fd, MemoryCheckpoint::kPrepare)) return;
    std::fprintf(
        stderr,
        "[sampling] request=%s choice=%d/%d temperature=%.17g top_p=%.17g "
        "logprobs=%d path=%s\n",
        request_id.c_str(), choice_index, sampling.choices,
        sampling.temperature, sampling.top_p, sampling.logprobs,
        plain_greedy ? "greedy" : "target_logits");
    TextStopStream text_stop(step_plan.stops);
    ChatResponseParser response_parser;
    s = response_parser.Init(
        req, prompt,
        sampling.choices == 1
            ? request_id
            : request_id + "_choice_" + std::to_string(choice_index),
        (collect_chat && !responses) || compaction_step || moderation_step,
        step_plan.legacy_output);
    if (!s.ok()) {
      SendError(fd, 400, s.message());
      return;
    }
    ReasoningOptions reasoning_options;
    s = ParseReasoningOptions(req, &reasoning_options);
    ThinkingBudget thinking_budget;
    const auto* request_tools = req.GetArray("tools");
    if (s)
      s = thinking_budget.Init(
          reasoning_options.token_budget, prompt, constraints.json_output,
          (request_tools && !request_tools->array.empty()) ||
              constraints.choice == OutputConstraints::ToolChoice::kNone,
          responses != nullptr || batch_usage);
    if (s && thinking_budget.Enabled()) {
      auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer, cancelled);
      if (!lock.owns_lock()) {
        count_abort();
        send_cancel_error("request cancelled waiting for thinking tokenizer");
        return;
      }
      const auto encode = [&](std::string_view text,
                              std::vector<int32_t>* out) {
        std::vector<uint32_t> ids;
        auto status = tok_->Encode(text, &ids, {.cancelled = cancelled});
        out->assign(ids.begin(), ids.end());
        return status;
      };
      const auto decode = [&](int32_t token, std::string* out) {
        if (cancelled())
          return Status::Fail("thinking budget preparation cancelled");
        return tok_->Decode({static_cast<uint32_t>(token)}, true, out);
      };
      s = thinking_budget.PrepareTokens(encode, decode, tok_->VocabSize(),
                                        stop_token_ids_);
    }
    if (!s) {
      if (cancelled()) {
        count_abort();
        send_cancel_error("request cancelled during thinking preparation");
      } else {
        SendError(fd, 500, s.message(), {}, "thinking_budget_unavailable");
      }
      return;
    }

    TokenSampler sampler;
    if (!plain_greedy) {
      s = sampler.Init(sampling, model_.Get().cfg.vocab, entropy, choice_index,
                       planning_config.token_vocab);
      if (!s) {
        SendError(fd, 400, s.message(), {}, "invalid_sampling_parameters");
        return;
      }
    }
    std::vector<uint8_t> sampling_allowed;
    if (!plain_greedy) {
      sampling_allowed.assign(model_.Get().cfg.vocab, 0);
      std::fill_n(sampling_allowed.begin(), planning_config.token_vocab, 1);
      // Added non-emitting control tokens cannot become invisible text. EOS
      // remains a real selectable stop token; grammar may further restrict it.
      for (int token = static_cast<int>(tok_->BaseVocabSize());
           token < planning_config.token_vocab; ++token) {
        std::string piece;
        s = tok_->Decode({static_cast<uint32_t>(token)}, !completion, &piece);
        if (!s) {
          SendError(fd, 500, s.message(), {}, "sampling_vocabulary_failed");
          return;
        }
        if (piece.empty() &&
            std::find(stop_token_ids_.begin(), stop_token_ids_.end(), token) ==
                stop_token_ids_.end())
          sampling_allowed[token] = 0;
      }
    }
    SampledToken selected_sample;
    TokenLogprobs token_logprobs(
        collect_chat && !responses, completion,
        completion && completion_plan.echo
            ? Utf8Characters(completion_prompt->echo_text)
            : 0,
        responses != nullptr);
    PredictionCursor prediction(prompt_budget.prediction_tokens);
    uint64_t prediction_pairs = 0;
    uint64_t prediction_pair_matches = 0;
    const auto start_stream = [&] {
      std::string response_start;
      if (responses) {
        const auto started = begin_response(&response_start);
        if (!started.ok()) {
          SendError(fd, started.http_status, started.ClientMessage(), {},
                    started.ClientCode());
          return false;
        }
      }
      if (compaction_step) {
        std::string compact_start;
        const auto started = responses->StartCompaction(&compact_start);
        if (!started.ok()) {
          SendError(fd, started.http_status, started.ClientMessage(), {},
                    started.ClientCode());
          return false;
        }
        response_start += compact_start;
      }
      if (!stream) return true;
      if (cancelled()) {
        count_abort();
        send_cancel_error("request cancelled before stream");
        return false;
      }
      const detail::WriteOptions options{.control = control.get()};
      if ((!headers_started &&
           !detail::BeginStreamingResponse(fd, response_state, options)) ||
          !WriteAll(
              fd,
              responses ? response_start
              : completion
                  ? CompletionChunk(id, model_field, created, choice_index, "",
                                    "", "null", include_usage,
                                    completion_plan.include_obfuscation)
                  : SseChunk(id, model_field, "assistant", "", "", choice_index,
                             created, include_usage),
              options)) {
        control->Cancel(RequestControl::State::kDisconnect);
        count_abort();
        return false;
      }
      if (!headers_started && http) http->StartHeartbeats(control);
      headers_started = true;
      return true;
    };

    // The outer lease outlives this grammar, including exception unwinding.
    std::unique_ptr<StructuredDecoder> structured;
    if (constraints.active) {
      if (!CheckMemoryAdmission(fd, MemoryCheckpoint::kPrepare)) return;
      if (cancelled()) {
        count_abort();
        send_cancel_error("request cancelled before structured admission");
        return;
      }
      diagnostic.Stage("structured_admission");
      const auto admission = [&] {
        RequestDiagnostics::Scope waiting(diagnostic, Wait::kGrammar);
        return structured_lease.Acquire(cancelled);
      }();
      if (admission == StructuredRequestLease::Result::kCancelled) {
        count_abort();
        send_cancel_error("request cancelled during structured handoff");
        return;
      }
      if (admission == StructuredRequestLease::Result::kBusy) {
        metrics_.structured_rejected.fetch_add(1, std::memory_order_relaxed);
        if (moderation_step) {
          if (!review_failed("structured_capacity_exhausted",
                             "Classification grammar capacity exhausted"))
            return;
          continue;
        }
        SendError(fd, 503, "structured request capacity exhausted", {},
                  "structured_capacity_exhausted");
        return;
      }
      if (detail::GrammarCleanupPending()) {
        metrics_.structured_rejected.fetch_add(1, std::memory_order_relaxed);
        if (moderation_step) {
          if (!review_failed("structured_capacity_exhausted",
                             "Classification grammar cleanup pending"))
            return;
          continue;
        }
        SendError(fd, 503,
                  "grammar worker cleanup still owns structured capacity", {},
                  "structured_capacity_exhausted");
        return;
      }
      if (!start_stream()) return;
      const auto prepare_start = std::chrono::steady_clock::now();
      metrics_.structured_preparing.fetch_add(1, std::memory_order_relaxed);
      ScopeExit prepared([&] {
        metrics_.structured_vocab_bytes.store(0, std::memory_order_relaxed);
        metrics_.structured_preparing.fetch_sub(1, std::memory_order_relaxed);
        metrics_.structured_prepare_seconds.Observe(
            std::chrono::duration<double>(std::chrono::steady_clock::now() -
                                          prepare_start)
                .count());
      });
      diagnostic.Stage("grammar_compile");
      // Decode is immutable. Use its exact raw bytes, including partial UTF-8
      // token pieces; never normalize or re-encode a token for the mask.
      const auto vocab_size = tok_->VocabSize();
      if (vocab_size <= 0 || vocab_size > 300000) {
        metrics_.structured_prepare_failed.fetch_add(1);
        if (moderation_step) {
          if (!review_failed("constraint_compile_failed",
                             "Invalid classification vocabulary"))
            return;
          continue;
        }
        SendError(fd, 400, "invalid structured vocabulary size", {},
                  "constraint_compile_failed");
        return;
      }
      std::vector<std::string> vocabulary(vocab_size);
      VocabularyStorageBudget storage;
      if (!storage.Charge(vocabulary.capacity() * sizeof(std::string))) {
        metrics_.structured_prepare_failed.fetch_add(1);
        if (moderation_step) {
          if (!review_failed("constraint_compile_failed",
                             "Classification vocabulary capacity exhausted"))
            return;
          continue;
        }
        SendError(fd, 400, "structured vocabulary storage budget exceeded", {},
                  "constraint_compile_failed");
        return;
      }
      std::vector<uint32_t> one(1);
      for (uint32_t token = 0; token < vocabulary.size(); ++token) {
        if (token % 256 == 0 && cancelled()) {
          count_abort();
          send_cancel_error("request cancelled before grammar compilation");
          return;
        }
        one[0] = token;
        s = tok_->Decode(one, true, &vocabulary[token]);
        if (!s) break;
        if (!storage.Charge(vocabulary[token].capacity() + 1)) {
          s = Status::Fail("structured vocabulary storage exceeds 32 MiB");
          break;
        }
        if (token % 256 == 0)
          metrics_.structured_vocab_bytes.store(storage.Used());
      }
      GrammarControl grammar_control;
      grammar_control.counters = metrics_.structured_workers;
      grammar_control.cancelled = cancelled;
      grammar_control.compile_deadline =
          prepare_start +
          std::chrono::milliseconds(kGrammarCompileMilliseconds);
      grammar_control.compile_started = [&] {
        metrics_.structured_compiles_started.fetch_add(1);
      };
      grammar_control.worker_ready = [&] {
        metrics_.structured_workers_ready.fetch_add(1);
      };
      grammar_control.worker_finished = [](const GrammarWorkerReport& report) {
        std::fprintf(
            stderr,
            "[q4t] grammar worker reaped=%d lifetime_max_rss_bytes=%lu "
            "reported_post_exec_rss_bytes=%lu "
            "wait_status=%d\n",
            report.reaped, report.max_rss_bytes, report.reported_rss_bytes,
            report.exit_status);
      };
      GrammarFailure compile_failure = GrammarFailure::kNone;
      if (s) {
        RequestDiagnostics::Scope waiting(diagnostic, Wait::kGrammar);
        s = StructuredDecoder::Create(
            constraints, vocabulary, model_.Get().cfg.vocab, stop_token_ids_,
            &structured, grammar_control, &compile_failure);
      }
      if (cancelled()) {
        count_abort();
        send_cancel_error("request cancelled during grammar compilation");
        return;
      }
      if (!s) {
        metrics_.structured_prepare_failed.fetch_add(1,
                                                     std::memory_order_relaxed);
        if (moderation_step) {
          const auto* code = compile_failure == GrammarFailure::kTimeout
                                 ? "constraint_compile_timeout"
                             : compile_failure == GrammarFailure::kResource
                                 ? "constraint_resource_exhausted"
                             : compile_failure == GrammarFailure::kDependency
                                 ? "constraint_dependency_unavailable"
                             : compile_failure == GrammarFailure::kWorker
                                 ? "constraint_worker_failed"
                             : compile_failure == GrammarFailure::kUnavailable
                                 ? "structured_capacity_exhausted"
                                 : "constraint_compile_failed";
          if (compile_failure == GrammarFailure::kTimeout)
            metrics_.structured_compile_timeouts.fetch_add(1);
          if (compile_failure == GrammarFailure::kResource)
            metrics_.structured_resource_exhausted.fetch_add(1);
          if (!review_failed(code, s.message())) return;
          continue;
        }
        if (compile_failure == GrammarFailure::kTimeout) {
          metrics_.structured_compile_timeouts.fetch_add(1);
          SendError(fd, 503, s.message(), {}, "constraint_compile_timeout");
        } else if (compile_failure == GrammarFailure::kResource) {
          metrics_.structured_resource_exhausted.fetch_add(1);
          SendError(fd, 503, s.message(), {}, "constraint_resource_exhausted");
        } else if (compile_failure == GrammarFailure::kDependency) {
          SendError(fd, 503, s.message(), {},
                    "constraint_dependency_unavailable");
        } else if (compile_failure == GrammarFailure::kWorker) {
          SendError(fd, 502, s.message(), {}, "constraint_worker_failed");
        } else if (compile_failure == GrammarFailure::kUnavailable) {
          metrics_.structured_rejected.fetch_add(1, std::memory_order_relaxed);
          SendError(fd, 503, s.message(), {}, "structured_capacity_exhausted");
        } else {
          SendError(fd, 400, s.message(), {}, "constraint_compile_failed");
        }
        return;
      }
    }
    if (structured) response_parser.SetSchemaReceipts(&structured->Receipts());
    if (!constraints.active && !start_stream()) return;

    std::vector<int32_t> ids;
    const bool explicit_slots = !seq_capacities_.empty();
    if (!prompt_encoded) s = encode_prompt();
    if (!s) {
      if (cancelled()) {
        count_abort();
        send_cancel_error("request cancelled during encoding");
      } else {
        SendError(fd, 400, s.message(), {}, planning_error.code,
                  planning_error.param);
      }
      return;
    }
    ids.assign(prompt_u32.begin(), prompt_u32.end());
    PromptCachePlan cache_plan;
    bool cache_available = prefix_cache_.Health().available;
    if (api_cache && !internal_step) {
      // Visual token expansion needs a media-aware state fingerprint (A10).
      // Never route images through the text-only cached prefill path.
      bool mapped = items.empty();
      cache_available = cache_available && mapped;
      if (items.empty() && prompt_u32.size() >= 1024) {
        std::vector<size_t> offsets;
        Status planned;
        {
          auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer, cancelled);
          planned = lock.owns_lock()
                        ? tok_->EchoByteOffsets(prompt, prompt_u32, true,
                                                 &offsets,
                                                 {.cancelled = cancelled})
                        : Status::Fail("Cache mapping cancelled");
        }
        std::span<const PromptCacheInputBoundary> boundaries;
        if (responses)
          boundaries = responses->Generation().input.cache_boundaries;
        if (planned) {
          if (const auto* messages = req.GetArray("messages")) {
            planned = PlanPromptCacheLayout(
                cache_options, *messages, prompt_layout, boundaries, offsets,
                prompt.size(), &cache_plan);
          } else {
            const PromptCacheBoundary raw{
                static_cast<int>(prompt_u32.size()) - 1, 0,
                PromptCacheBoundary::Kind::kEligibleMessage};
            planned = PlanPromptCache(cache_options, {&raw, 1},
                                       static_cast<int>(prompt_u32.size()),
                                       &cache_plan);
          }
        }
        if (!planned) {
          mapped = false;
          cache_available = false;
          std::fprintf(stderr,
                       "[prefix-cache] request=%s mapping=unavailable\n",
                       request_id.c_str());
        }
      }
      if (!mapped ||
          (cache_plan.reads.empty() && cache_plan.writes.empty()))
        prefix_scope.clear();
      if (prewarm && !mapped && prompt_u32.size() >= 1024) {
        SendError(fd, 503, "Prompt cache prewarm is unavailable", {},
                  "prompt_cache_unavailable", "prompt_cache_options.prewarm");
        return;
      }
    } else if (!internal_step && request_prefix_write_tokens > 0) {
      cache_plan.writes.push_back({request_prefix_write_tokens, 1800});
    }
    const auto next_cache_boundary = [&](int base, int count) {
      for (const auto& point : cache_plan.writes)
        if (point.tokens > base) return std::min(count, point.tokens - base);
      return count;
    };
    const auto cache_boundary = [&](int boundary) -> const PromptCacheWrite* {
      for (const auto& point : cache_plan.writes)
        if (point.tokens == boundary) return &point;
      return nullptr;
    };
    int cache_written_tokens = 0;
    const size_t required_capacity =
        prewarm ? prompt_u32.size() : prompt_budget.required_capacity;

    // B1 multi-request scheduling: each request runs on its own thread. All
    // GPU work uses the DEFAULT stream (the model's per-forward scratch is a
    // single shared allocation, and per-sequence state is pooled + isolated by
    // seq_id), so model forwards are serialized behind model_mu_ for both
    // scratch safety and GPU-stream ordering. GPU readback completes under
    // the lock; tokenization and SSE output use the completed host result.
    diagnostic.Stage("slot_admission");
    attempted_slot = true;
    int seq_id;
    {
      RequestDiagnostics::Scope wait(diagnostic, Wait::kSlot);
      seq_id = AllocSeqId(control.get(), fd, required_capacity);
    }
    if (seq_id == -5 || seq_id == -6) {
      const auto state =
          seq_id == -5 ? MemoryState::kLow : MemoryState::kUnavailable;
      diagnostic.Outcome("error", kMemoryStates[static_cast<size_t>(state)]);
      RejectMemoryAdmission(fd, state, MemoryCheckpoint::kExecute);
      return;
    }
    if (seq_id == -4) {
      diagnostic.Outcome("error", "queue_capacity_exhausted");
      // SendError uses the existing response context: JSON 503 before headers,
      // one SSE error terminal after headers, never a second HTTP response.
      SendError(fd, 503, "slot waiting capacity exhausted", {},
                "queue_capacity_exhausted");
      return;
    }
    if (seq_id == -3) {
      RejectUnhealthy(fd);
      return;
    }
    if (seq_id == -2) {
      count_abort();
      send_cancel_error("request cancelled before admission");
      return;
    }
    if (seq_id < 0) {
      SendError(fd, 503, "server shutting down");
      return;
    }
    diagnostic.Admitted(seq_id);
    if (internal) {
      if (!internal->accounting.StartStep()) {
        FreeSeqId(seq_id);
        SendError(fd, 500, "Invalid internal model accounting state", {},
                  "file_preparation_state_invalid");
        return;
      }
    } else if (completion) {
      if (choice_index % completion_plan.best_of == 0)
        admitted_completion_prompt_tokens += prompt_u32.size();
      rate_lease.StartPrompt(admitted_completion_prompt_tokens);
    } else if (responses) {
      rate_lease.StartPrompt(prepared_input + responses_usage.input_tokens +
                             moderation_usage.input_tokens + prompt_u32.size());
    } else {
      rate_lease.Start();
    }
    const int request_capacity = model_.Get().cfg.Capacity(seq_id);
    if (explicit_slots)
      std::fprintf(stderr,
                   "[q4t][slot-admission] request=%s slot=%d capacity=%d "
                   "required=%zu\n",
                   request_id.c_str(), seq_id, request_capacity,
                   required_capacity);
    diagnostic.Stage("encode");
    if (choice_index == 0)
      metrics_.queue_seconds.Observe(
          std::chrono::duration<double>(std::chrono::steady_clock::now() -
                                        t_arrive)
              .count());
    // Per-request device buffers (freed by `cleanup` on any exit path). Prefill
    // logits use the shared d_prefill_logits_ (under model_mu_), not a per-
    // request buffer.
    uint16_t* d_trunk_full = nullptr;
    uint16_t* d_vfeats = nullptr;
    uint16_t* d_session_trunk = nullptr;
    uint16_t* d_prediction_logits = nullptr;
    uint16_t* d_prompt_logits = nullptr;
    uint16_t* d_mtp_g =
        nullptr;  // MTP rolling draft trunk [hc*hs] (Stage 2c:
                  // per-request, so concurrent MTP requests don't
                  // share the legacy d_g_/d_g_next_ double buffer)
    trace::RequestOutcome trace_outcome = trace::RequestOutcome::kFailed;
    uint64_t trace_output_tokens = 0;
    bool buffers_ok = true;
    const auto release_buffer = [&](auto*& pointer, const char* site) {
      const bool allocated = pointer != nullptr;
      RequestDiagnostics::ExecutionScope running(diagnostic,
                                                 ProgressStage::kCleanup);
      const auto result =
          detail::ReleaseCudaMemory(pointer, site, request_id.c_str());
      if (allocated && result.ok()) running.Completed();
      RecordDeviceMemoryFailure(result, site);
      buffers_ok = buffers_ok && result.ok();
      if (!result.ok()) {
        const std::lock_guard<std::mutex> lock(model_mu_);
        AbortBackendExecution();
      }
    };
    const auto release_buffers = [&] {
      release_buffer(d_prediction_logits, "request.prediction_logits_free");
      release_buffer(d_prompt_logits, "request.prompt_logits_free");
      release_buffer(d_session_trunk, "request.session_trunk_free");
      release_buffer(d_trunk_full, "request.prompt_trunk_free");
      release_buffer(d_vfeats, "request.vision_features_free");
      release_buffer(d_mtp_g, "request.draft_trunk_free");
    };
    const auto allocate_buffer = [&](auto*& pointer, size_t bytes,
                                     const char* site) {
      RequestDiagnostics::ExecutionScope running(diagnostic,
                                                 ProgressStage::kCache);
      const auto result =
          detail::AllocateCudaMemory(pointer, bytes, site, request_id.c_str());
      if (result.ok()) running.Completed();
      RecordDeviceMemoryFailure(result, site);
      if (result.state == DeviceMemoryState::kFailure) {
        const std::lock_guard<std::mutex> lock(model_mu_);
        AbortBackendExecution();
      }
      return result;
    };
    const auto allocation_error = [&](const DeviceMemoryResult& result) {
      err_guard.ok = false;
      diagnostic.Outcome("error", result.Reason());
      SendError(fd, 503,
                result.quota_exhausted ? "CUDA allocation quota exhausted"
                : result.exhausted()   ? "device memory exhausted"
                                     : "device memory failure; restart server",
                {}, result.exhausted() ? result.Reason() : "gpu_unhealthy");
    };
    bool cleaned = false;
    auto cleanup = [&]() {
      if (cleaned) return;
      cleaned = true;
      release_buffers();
      if (router_trace_)
        router_trace_->EndRequest(control->Cancelled()
                                      ? trace::RequestOutcome::kCancelled
                                      : trace_outcome,
                                  trace_output_tokens);
      FreeSeqId(seq_id);
      diagnostic.Released();
    };
    ScopeExit buffer_cleanup(cleanup);
    detail::ServeFaultPoint("handler.after_slot");

    if (cancelled()) {
      count_abort();
      cleanup();
      send_cancel_error("request cancelled before encoding");
      return;
    }

    // 1b. Multimodal: each <|image_pad|> (image_token_id) / <|video_pad|>
    // (video_token_id) placeholder expands to the number of merged visual
    // tokens for its item, and the vision tower's features are injected in
    // place of those token embeddings (in prompt position order).
    model::VisionFeatures vfeats;
    if (!items.empty()) {
      diagnostic.Stage("media");
      if (ids.size() >= static_cast<size_t>(request_capacity)) {
        SendError(fd, 400, "expanded media exceeds assigned context", {},
                  "context_length_exceeded", "messages");
        return;
      }
      std::vector<int> counts;
      std::vector<std::array<int, 3>> grids;
      std::string verr;
      DeviceMemoryResult vision_memory;
      // The vision tower uses a shared workspace, so its forward is serialized
      // behind model_mu_ (the same lock that serializes the main model). The
      // pipeline runs on the default stream and self-synchronizes internally.
      {
        auto lock = diagnostic.Lock(model_mu_, Wait::kModel, cancelled);
        if (!lock.owns_lock()) {
          count_abort();
          send_cancel_error("request cancelled waiting for vision execution");
          return;
        }
        ProgressExecution progress_execution(ProgressWork::kMedia);
        OnException backend_failure([&] { AbortBackendExecution(); });
        if (RejectUnhealthy(fd)) {
          // Outer buffer cleanup runs after this model lock is released.
          return;
        }
        if (!RunVisionPipeline(items, media_plan, &d_vfeats, &vfeats.num_tokens,
                               &counts, &grids, &verr, &vision_memory,
                               request_id.c_str(), cancelled,
                               &planning_error)) {
          if (!vision_memory.ok()) {
            SendError(fd, 503, "vision device resource failure", {},
                      vision_memory.Reason());
          } else if (cancelled()) {
            count_abort();
            send_cancel_error("request cancelled during media processing");
          } else {
            const bool mismatch = planning_error.code == "media_plan_mismatch";
            SendError(fd, mismatch ? 500 : 400, verr, {}, planning_error.code,
                      planning_error.param);
          }
          // A failed buffer release may need to reacquire model_mu_ to drain.
          return;
        }
      }
      vfeats.grids = std::move(grids);
      vfeats.device = d_vfeats;
    }
    const int T = static_cast<int>(ids.size());
    if (T > request_capacity || (T == request_capacity && max_tokens > 0)) {
      SendError(fd, 400,
                "prompt too long for context: " + std::to_string(T) +
                    " tokens >= " + std::to_string(request_capacity) +
                    " max_len");
      cleanup();
      return;
    }

    const int vocab = model_.Get().cfg.vocab;
    const auto is_stop_token = [this](int32_t token) {
      return std::find(stop_token_ids_.begin(), stop_token_ids_.end(), token) !=
             stop_token_ids_.end();
    };
    // Chunked prefill (262K context, see PHASES.md 262K memory budget): the
    // forward workspace (d_ws, d_trunk, ...) is sized for max_prefill tokens,
    // so a prompt longer than max_prefill is run in chunks of max_prefill.
    // max_prefill is now the CHUNK size, not a prompt cap (the prompt cap is
    // max_len). Text-only: a vision prompt's special (t,h,w) rope is not
    // reproducible by the continuation chunks' text rope, so vision + chunked
    // is rejected (vision prompts are far shorter than 262K in practice).
    const int chunk = max_prefill_;
    const bool chunked = T > chunk;
    if (chunked && vfeats.num_tokens > 0) {
      SendError(fd, 400,
                "chunked prefill (prompt > max_prefill) does not support "
                "vision input");
      cleanup();
      return;
    }
    if (router_trace_) router_trace_->BeginRequest(ids, request_id);
    // Chunk 0 is a true prefill (state reset + rope table + PLE history);
    // chunks 1.. are ModelDecodeBatch calls that CONTINUE from the existing
    // per-layer state (linear SSM/conv recurrence, full-attention KV/indexer
    // written at absolute positions) — bit-identical to one big prefill. Only
    // the LAST chunk's final logits row is needed (first decode token), so
    // all serve paths explicitly select that row before the output head.
    // Shared d_prefill_logits_ holds [max_seq, vocab], including inline
    // fallback, vision and MTP main-model prefill (one row per request).
    // MTP: prefill trunk_out buffer (pre-final-mixer multi stream [T, hc*hs])
    // for the draft-extend. Allocated when MTP is loaded, for BOTH one-shot and
    // chunked prefill. The chunked path accumulates the trunk across chunks
    // (each chunk's trunk_out writes its [base, base+c) rows); the draft-extend
    // then runs over the full prompt in chunks (MtpDraftExtend's chunked path).
    // The draft KV/indexer is allocated at MTP load time (pooled over max_seq,
    // max_len), so it is NOT a per-request cost -- the only per-request extra
    // is this trunk buffer (T * hc_dim * 2, ~0.9 GB at 44K, ~5.4 GB at 262K).
    // If the allocation fails (not enough headroom), fall back to plain decode.
    const size_t trunk_hc_dim = mtp_loaded_
                                    ? static_cast<size_t>(mtp_.cfg.hc) *
                                          static_cast<size_t>(mtp_.cfg.hs)
                                    : 0;
    const bool request_mtp =
        mtp_loaded_ && plain_greedy && !structured && !prediction_enabled &&
        (max_tokens > 0 || prewarm);
    const bool stream_mtp =
        request_mtp && (explicit_slots || (api_cache && !prefix_scope.empty()));
    const bool target_cache =
        !request_mtp && (session.cache || !prefix_scope.empty());
    const SessionLayout cache_layout =
        request_mtp ? SessionLayout::kMtp : SessionLayout::kTarget;
    int32_t stream_d0 = -1;
    bool stream_draft_ready = false;
    if (request_mtp) {
      // Advisory preflight for optional MTP buffers. Unknown or insufficient
      // global MemAvailable declines the allocation. This is not a reservation:
      // concurrent allocations and driver accounting can still exhaust RAM.
      const size_t trunk_bytes =
          static_cast<size_t>(stream_mtp ? std::min(T, chunk) : T) *
          trunk_hc_dim * 2;
      const size_t draft_logits_bytes =
          static_cast<size_t>(max_prefill_) *
          static_cast<size_t>(model_.Get().cfg.vocab) * 2;
      const size_t kPreflightMargin = 2u * 1024u * 1024u * 1024u;  // 2 GB
      const size_t need = trunk_bytes + draft_logits_bytes + kPreflightMargin;
      const auto memory = runtime::ReadSystemMemory();
      if (!memory.CanAllocate(need)) {
        std::fprintf(
            stderr,
            "[q4t] MTP memory preflight: valid=%d MemAvailable=%.2f GB "
            "need=%.2f GB (trunk T=%d); declining optional allocation\n",
            memory.valid, memory.available_bytes / 1e9, need / 1e9, T);
        d_trunk_full = nullptr;
      } else {
        const auto allocated =
            allocate_buffer(d_trunk_full, trunk_bytes, "request.prompt_trunk");
        if (!allocated.ok() && !allocated.exhausted()) {
          allocation_error(allocated);
          return;
        }
        if (allocated.exhausted())
          std::fprintf(
              stderr,
              "[q4t] trunk allocation exhausted for T=%d; plain decode\n", T);
      }
    }
    if (stream_mtp) {
      const auto allocated =
          d_trunk_full ? allocate_buffer(d_mtp_g, trunk_hc_dim * 2,
                                         "request.stream_draft_trunk")
                       : DeviceMemoryResult{DeviceMemoryState::kExhausted};
      if (!allocated.ok()) {
        allocation_error(allocated);
        return;
      }
    }
    std::vector<uint16_t> h_logits(static_cast<size_t>(vocab));
    const int prompt_score_rows = std::min(16, max_prefill_);
    std::vector<uint16_t> h_prompt_logits;
    if (score_prompt) {
      const size_t elements = static_cast<size_t>(prompt_score_rows) * vocab;
      const auto allocated = allocate_buffer(d_prompt_logits, elements * 2,
                                              "request.prompt_logits");
      if (!allocated.ok()) {
        allocation_error(allocated);
        return;
      }
      h_prompt_logits.resize(elements);
    }
    std::vector<uint16_t> h_prediction_logits;
    bool prediction_logits_ready = false;
    if (prediction_enabled && !prompt_budget.prediction_tokens.empty()) {
      const auto allocated =
          allocate_buffer(d_prediction_logits, static_cast<size_t>(vocab) * 4,
                          "request.prediction_logits");
      if (!allocated.ok()) {
        allocation_error(allocated);
        return;
      }
      h_prediction_logits.resize(static_cast<size_t>(vocab) * 2);
    }

    auto argmax = [&](const uint16_t* h) {
      int best = 0;
      float best_v = -1e30f;
      for (int v = 0; v < vocab; ++v) {
        const uint32_t bits = static_cast<uint32_t>(h[v]) << 16;
        float f;
        std::memcpy(&f, &bits, sizeof(f));
        if (f > best_v) {
          best_v = f;
          best = v;
        }
      }
      return best;
    };

    // Capture only the selected request's execution, after tokenization and
    // allocation. The observer is disabled unless explicitly requested.
    q4t::diagnostic::ProfileCapture execution_capture(T);
    diagnostic.Stage("prefill");
    // 2. Prefill (PD-ready 阶段边界 API: Begin -> Prefill). The forward uses
    // the shared per-forward scratch, so it is serialized behind model_mu_; the
    // per-sequence recurrent state is isolated by seq_id. All GPU work uses the
    // default stream, so the forward is ordered with the D2H below.
    model::ModelSequence seq;
    ScopeExit sequence_cleanup([&] {
      if (std::uncaught_exceptions()) {
        if (seq.HasPending()) {
          auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
          RequestDiagnostics::ExecutionScope running(
              diagnostic, ProgressStage::kCleanup);
          AbortBackendExecution();
          seq.FailAfterDrain();
        }
        seq.Fail();
      }
      model::ModelEndSequence(&seq);
    });
    const model::VisionFeatures* vptr =
        (vfeats.num_tokens > 0) ? &vfeats : nullptr;
    // Batched prefill (plain path): non-vision, non-chunked, non-MTP requests
    // pack their prefill with other concurrent requests into ONE
    // ModelPrefillBatch (the dense weights read once for the whole batch
    // instead of per request). MTP needs the prefill trunk_out (draft-extend)
    // and vision/chunked need the per-request path, so those still prefill
    // inline.
    const bool batched_prefill = scheduler_active_ && !request_mtp &&
                                 !score_prompt &&
                                 !chunked && vptr == nullptr &&
                                 getenv("Q4T_NO_BATCH_PREFILL") == nullptr;
    const bool scheduled_chunk_prefill =
        scheduler_active_ && !request_mtp && !score_prompt && chunked &&
        vptr == nullptr;
    bool prefill_cancelled = false;
    if (stream_mtp && (session.cache || !prefix_scope.empty())) {
      const auto allocated = allocate_buffer(d_session_trunk, trunk_hc_dim * 2,
                                             "request.session_trunk");
      if (!allocated.ok()) {
        if (api_cache && !session.cache && allocated.exhausted()) {
          // Optional cache scratch is not a model health dependency. The
          // prefill still runs normally; prewarm reports the failed write.
          prefix_scope.clear();
          cache_available = false;
        } else {
          allocation_error(allocated);
          return;
        }
      }
    }
    int restored_prefix = 0;
    bool session_gpu_error = false;
    const auto session_copy = [&](void* host, void* state, size_t bytes,
                                  bool restore) -> Status {
      RequestDiagnostics::ExecutionScope running(diagnostic,
                                                 ProgressStage::kCache);
      const auto error =
          restore ? cudaMemcpy(state, host, bytes, cudaMemcpyHostToDevice)
                  : cudaMemcpy(host, state, bytes, cudaMemcpyDeviceToHost);
      if (error == cudaSuccess) {
        // Pageable H2D may return before DMA completes. Only D2H is an
        // existing completion boundary; restore completes at later readback.
        if (!restore) running.Completed();
        return Status();
      }
      session_gpu_error = true;
      gpu_healthy_.store(false, std::memory_order_relaxed);
      return Status::Fail(std::string("session CUDA copy: ") +
                          cudaGetErrorString(error));
    };
    // Call only after BeginSequence, under the model lock. Exact token prefixes
    // and execution layouts are checked before any state bytes are copied.
    const auto restore_cache = [&] {
      RequestDiagnostics::Scope waiting(diagnostic, Wait::kCache);
      const auto begin = std::chrono::steady_clock::now();
      int prefix = session.cache
                       ? session_cache_.Match(session.key, ids, cache_layout)
                       : 0;
      SessionCache* selected_cache = &session_cache_;
      std::string selected_key = session.key;
      SessionLease shared_read;
      if (!prefix_scope.empty() && (!api_cache || !cache_plan.reads.empty())) {
        const int shared = prefix_cache_.MatchShared(
            prefix_scope, ids, &shared_read.key, cache_layout,
            api_cache ? std::span<const int>(cache_plan.reads)
                      : std::span<const int>());
        if (shared > 0) shared_read.cache = &prefix_cache_;
        if (shared > prefix) {
          prefix = shared;
          selected_cache = &prefix_cache_;
          selected_key = shared_read.key;
        }
      }
      Status restored;
      if (prefix > 0) {
        std::vector<SessionRange> ranges;
        restored = target_cache
                       ? TargetSessionStateRanges(model_.Get(), seq_id, prefix,
                                                  &ranges)
                       : SessionStateRanges(model_.Get(), mtp_, seq_id, prefix,
                                            d_mtp_g, d_session_trunk, &ranges);
        int32_t cached_pending = -1;
        if (restored.ok())
          restored = selected_cache->Restore(selected_key, ranges, session_copy,
                                             cancelled, &stream_d0,
                                             &cached_pending, cache_layout);
        if (restored.ok() && !target_cache && cached_pending != ids[prefix]) {
          RequestDiagnostics::ExecutionScope running(diagnostic,
                                                     ProgressStage::kCache);
          // The draft is shifted by one token. A tool/message boundary may
          // replace its final predicted token; target state remains valid.
          const int position = prefix - 1;
          restored = mtp::MtpDraftExtend(mtp_, &ids[prefix], d_session_trunk,
                                         &position, 1, nullptr, nullptr,
                                         nullptr, seq_id, false);
          restored =
              FinishHostReadback(cudaSuccess, &gpu_healthy_, {}, restored,
                                 nullptr, &metrics_.execution_steps_total);
          if (!restored.ok()) session_gpu_error = true;
        }
        if (restored.ok()) {
          seq.history.assign(ids.begin(), ids.begin() + prefix);
          seq.position = prefix;
          model_.Get().rope_delta[seq_id] = 0;
          restored_prefix = prefix;
        } else if (session_gpu_error) {
          s = restored;
        } else {
          // An incomplete read may have written part of the destination.
          // Reset every partially restored model before falling back.
          RequestDiagnostics::ExecutionScope running(diagnostic,
                                                     ProgressStage::kCache);
          s = model::ModelBeginSequence(model_.Get(), &seq, nullptr, seq_id);
          if (s.ok() && !target_cache)
            s = mtp::MtpResetState(mtp_, nullptr, seq_id);
        }
      }
      std::fprintf(stderr,
                   "[session-cache] request=%s event=restore prefix=%d "
                   "ms=%.3f error=%s source=%s layout=%s\n",
                   request_id.c_str(), restored_prefix,
                   std::chrono::duration<double, std::milli>(
                       std::chrono::steady_clock::now() - begin)
                       .count(),
                   restored.ok() ? "none" : restored.message().c_str(),
                   selected_cache == &prefix_cache_ ? "shared" : "session",
                   target_cache ? "target" : "mtp");
    };
    const auto save_target = [&](SessionCache* cache, const std::string& key,
                                 const char* kind,
                                 SessionCacheLifetime lifetime = {}) {
      RequestDiagnostics::Scope waiting(diagnostic, Wait::kCache);
      const auto begin = std::chrono::steady_clock::now();
      SessionSaveTiming timing(request_id.c_str(), kind);
      std::vector<SessionRange> ranges;
      Status saved;
      {
        SessionSaveTiming::Scope span(&timing, SessionSaveTiming::kRanges);
        saved = TargetSessionStateRanges(model_.Get(), seq_id, seq.position,
                                         &ranges);
      }
      if (saved.ok() && !seq.HasPending() &&
          seq.history.size() == static_cast<size_t>(seq.position) &&
          model_.Get().rope_delta[seq_id] == 0) {
        std::vector<int32_t> prefix(seq.history);
        prefix.push_back(-1);  // No draft token is part of a target snapshot.
        saved = cache->Save(key, prefix, ranges, session_copy, cancelled, -1,
                            &timing, SessionLayout::kTarget, lifetime);
        if (saved.ok()) detail::ServeSnapshotTokens(request_id.c_str(), prefix);
      } else if (saved.ok()) {
        saved = Status::Fail("incomplete target session boundary");
      }
      timing.Report(saved.ok());
      std::fprintf(stderr,
                   "[session-cache] request=%s event=save prefix=%d "
                   "ms=%.3f disk_bytes=%zu error=%s layout=target source=%s\n",
                   request_id.c_str(), seq.position,
                   std::chrono::duration<double, std::milli>(
                       std::chrono::steady_clock::now() - begin)
                       .count(),
                   cache->Bytes(),
                   saved.ok() ? "none" : saved.message().c_str(), kind);
      return saved;
    };
    const auto save_shared = [&](int boundary) {
      const auto* point = cache_boundary(boundary);
      if (!point || prefix_scope.empty() || boundary >= T) return;
      const auto digest =
          trace::Sha256({reinterpret_cast<const uint8_t*>(ids.data()),
                         static_cast<size_t>(boundary) * sizeof(int32_t)});
      const std::string snapshot_key =
          prefix_scope + (target_cache ? ":target-v1:" : ":") +
          trace::HexDigest(digest);
      if (!prefix_cache_.AcquireNew(snapshot_key,
                                     api_cache ? point->ttl_seconds : 0))
        return;
      auto lease = std::make_unique<SessionLease>();
      lease->cache = &prefix_cache_;
      lease->key = snapshot_key;
      shared_writes.push_back(std::move(lease));
      const SessionCacheLifetime lifetime =
          api_cache ? PromptCacheLifetime(cache_options, point->ttl_seconds,
                                           responses != nullptr)
                    : SessionCacheLifetime{};
      Status saved;
      if (target_cache) {
        saved = save_target(&prefix_cache_, snapshot_key, "shared", lifetime);
      } else {
        RequestDiagnostics::Scope waiting(diagnostic, Wait::kCache);
        SessionSaveTiming timing(request_id.c_str(), "shared");
        std::vector<SessionRange> ranges;
        {
          SessionSaveTiming::Scope span(&timing, SessionSaveTiming::kRanges);
          saved = SessionStateRanges(model_.Get(), mtp_, seq_id, boundary,
                                     d_mtp_g, d_session_trunk, &ranges);
        }
        if (saved)
          saved = prefix_cache_.Save(
              snapshot_key, {ids.data(), static_cast<size_t>(boundary + 1)},
              ranges, session_copy, cancelled, -1, &timing,
              SessionLayout::kMtp, lifetime);
        timing.Report(saved.ok());
      }
      // Track actual persisted coverage; terminal accounting removes the
      // prefix already counted as a read so nested snapshots do not double
      // count input tokens. This is local work accounting, not cloud pricing.
      if (saved) {
        cache_written_tokens = std::max(cache_written_tokens, boundary);
        cache_available = true;
      } else {
        cache_available = false;
      }
      if (session_gpu_error) s = saved;
      std::fprintf(stderr,
                   "[prefix-cache] request=%s event=write prefix=%d ttl=%d "
                   "saved=%d bytes=%zu\n",
                   request_id.c_str(), boundary, point->ttl_seconds, saved.ok(),
                   prefix_cache_.Bytes());
    };
    if (score_prompt) {
      // Teacher-forced prompt scores come from real target logits. Bound
      // scratch/readback to 16 rows independently of the prompt length.
      SamplingOptions scoring;
      scoring.temperature = 0;
      scoring.logprobs = true;
      scoring.top_logprobs = completion_plan.logprobs;
      TokenSampler prompt_sampler;
      s = prompt_sampler.Init(scoring, vocab, 0, 0, planning_config.token_vocab);
      const auto decode = [&](int32_t token, std::string* out) {
        return tok_->Decode({static_cast<uint32_t>(token)}, false, out);
      };
      if (s && completion_prompt->echo_begin == 0) {
        CompletionProbabilityRow row;
        s = CompletionProbability(ids[0], nullptr, decode, &row);
        if (s) s = prompt_probabilities.Add(row, completion_prompt->echo_offsets[0]);
      }
      if (s) {
        auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
        RequestDiagnostics::ExecutionScope running(diagnostic,
                                                   ProgressStage::kPrefill);
        OnException backend_failure([&] { AbortBackendExecution(); });
        s = ExecutionStatus();
        if (s) s = model::ModelBeginSequence(model_.Get(), &seq, nullptr, seq_id);
      }
      while (s && seq.position < T) {
        if (cancelled()) {
          prefill_cancelled = true;
          break;
        }
        const int base = seq.position;
        const int count = std::min(prompt_score_rows, T - base);
        {
          auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
          ProgressExecution progress_execution(diagnostic.Work());
          OnException backend_failure([&] { AbortBackendExecution(); });
          s = ExecutionStatus();
          if (router_trace_)
            router_trace_->BeginForward(trace::RouteStage::kPrefill, base, count);
          if (s)
            s = model::ModelPrefillTextChunk(
                model_.Get(), &seq, ids.data(), T, count, d_prompt_logits,
                nullptr, nullptr, model::LogitsRows::kAllRows,
                model::SequenceCompletion::kDeferred);
          cudaError_t copy_error = cudaSuccess;
          if (s)
            copy_error = cudaMemcpyAsync(
                h_prompt_logits.data(), d_prompt_logits,
                static_cast<size_t>(count) * vocab * 2,
                cudaMemcpyDeviceToHost, nullptr);
          model::ModelSequence* sequence = &seq;
          s = FinishHostReadback(copy_error, &gpu_healthy_, {&sequence, 1}, s,
                                 router_trace_.get(),
                                 &metrics_.execution_steps_total);
        }
        for (int row = 0; s && row < count; ++row) {
          const size_t next = static_cast<size_t>(base + row + 1);
          if (next < completion_prompt->echo_begin ||
              next >= completion_prompt->echo_end)
            continue;
          if (cancelled()) {
            prefill_cancelled = true;
            break;
          }
          SampledToken sample;
          s = prompt_sampler.Select(
              {h_prompt_logits.data() + static_cast<size_t>(row) * vocab,
               static_cast<size_t>(vocab)}, &sample, {}, ids[next]);
          CompletionProbabilityRow probability;
          if (s) s = CompletionProbability(ids[next], &sample, decode, &probability);
          if (s)
            s = prompt_probabilities.Add(
                probability, completion_prompt->echo_offsets[
                                 next - completion_prompt->echo_begin]);
        }
        if (prefill_cancelled) break;
        if (s && base + count == T)
          std::copy_n(h_prompt_logits.data() +
                          static_cast<size_t>(count - 1) * vocab,
                       vocab, h_logits.data());
      }
      if (!s) seq.Fail();
    } else if (stream_mtp) {
      q4t::diagnostic::Batch execution_batch(request_id.c_str(), seq_id, 0, T);
      q4t::diagnostic::Scope execution_prefill("handler_stream_mtp_prefill", 0,
                                               T);
      auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
      ProgressExecution progress_execution(diagnostic.Work());
      OnException backend_failure([&] { AbortBackendExecution(); });
      s = ExecutionStatus();
      if (s.ok())
        s = model::ModelBeginSequence(model_.Get(), &seq, nullptr, seq_id);
      if (s.ok()) s = mtp::MtpResetState(mtp_, nullptr, seq_id);
      if (s.ok() && (session.cache || !prefix_scope.empty())) restore_cache();
      if (s.ok() && restored_prefix > 0) save_shared(restored_prefix);
      std::vector<int32_t> shifted;
      std::vector<int> positions;
      int chunks = 0;
      while (s.ok() && seq.position < T) {
        if (cancelled()) {
          prefill_cancelled = true;
          break;
        }
        const int base = seq.position;
        const int count = next_cache_boundary(base, std::min(chunk, T - base));
        const int boundary = base + count;
        const bool last = boundary == T;
        const bool snapshot = !last && cache_boundary(boundary);
        if (router_trace_)
          router_trace_->BeginForward(trace::RouteStage::kPrefill, base, count);
        s = model::ModelPrefillTextChunk(
            model_.Get(), &seq, ids.data(), T, count,
            last ? d_prefill_logits_ : nullptr, nullptr, d_trunk_full,
            model::LogitsRows::kLastRow, model::SequenceCompletion::kDeferred);
        cudaError_t copy_error = cudaSuccess;
        if (s.ok() && last)
          copy_error = cudaMemcpyAsync(h_logits.data(), d_prefill_logits_,
                                       static_cast<size_t>(vocab) * 2,
                                       cudaMemcpyDeviceToHost, nullptr);
        model::ModelSequence* sequence = &seq;
        s = FinishHostReadback(copy_error, &gpu_healthy_, {&sequence, 1}, s,
                               router_trace_.get(),
                               &metrics_.execution_steps_total);
        if (!s.ok()) break;
        // A prewarm has no next output token to sample. Earlier boundaries
        // already contain the complete draft state needed by later requests.
        if (prewarm && last) {
          ++chunks;
          break;
        }
        shifted.resize(count);
        positions.resize(count);
        for (int i = 0; i < count; ++i) {
          positions[i] = base + i;
          shifted[i] =
              base + i + 1 < T ? ids[base + i + 1] : argmax(h_logits.data());
        }
        s = mtp::MtpDraftExtend(mtp_, shifted.data(), d_trunk_full,
                                positions.data(), count, &stream_d0, d_mtp_g,
                                nullptr, seq_id, last, snapshot);
        // The target chunk was committed above. Drain a failed draft submission
        // before releasing this request's storage or giving scratch to a peer.
        s = FinishHostReadback(cudaSuccess, &gpu_healthy_, {}, s, nullptr,
                               &metrics_.execution_steps_total);
        if (s.ok() && (last || snapshot) && d_session_trunk) {
          const auto error = cudaMemcpy(
              d_session_trunk,
              d_trunk_full + static_cast<size_t>(count - 1) * trunk_hc_dim,
              static_cast<size_t>(trunk_hc_dim) * 2, cudaMemcpyDeviceToDevice);
          if (error != cudaSuccess) {
            gpu_healthy_.store(false, std::memory_order_relaxed);
            s = Status::Fail("session prefill final trunk copy failed");
          }
        }
        if (s.ok() && snapshot) save_shared(boundary);
        ++chunks;
      }
      if (s.ok() && !prefill_cancelled && !prewarm) {
        s = model::ModelReserveVerifyCheckpoints(model_.Get(), mtp_k_);
        if (s.ok()) s = mtp::MtpReserveScratch(mtp_, mtp_k_ + 1);
        stream_draft_ready = s.ok();
      }
      if (!s.ok()) {
        s = FinishHostReadback(cudaSuccess, &gpu_healthy_, {}, s, nullptr);
        seq.Fail();
      }
      std::fprintf(stderr,
                   "[mtp-prefill] request=%s streamed=1 chunks=%d "
                   "trunk_bytes=%zu ready=%d\n",
                   request_id.c_str(), chunks,
                   static_cast<size_t>(std::min(T, chunk)) * trunk_hc_dim * 2,
                   stream_draft_ready);
    } else if (target_cache) {
      // Cache state belongs to this request's slot. The packed prefill queue
      // cannot start at a restored prefix; use the existing chunk API instead.
      auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
      ProgressExecution progress_execution(diagnostic.Work());
      OnException backend_failure([&] { AbortBackendExecution(); });
      s = ExecutionStatus();
      if (s.ok())
        s = model::ModelBeginSequence(model_.Get(), &seq, nullptr, seq_id);
      if (s.ok()) restore_cache();
      if (s.ok() && restored_prefix > 0) save_shared(restored_prefix);
      while (s.ok() && seq.position < T) {
        if (cancelled()) {
          prefill_cancelled = true;
          break;
        }
        const int base = seq.position;
        const int count = next_cache_boundary(base, std::min(chunk, T - base));
        const int boundary = base + count;
        const bool last = boundary == T;
        if (router_trace_)
          router_trace_->BeginForward(trace::RouteStage::kPrefill, base, count);
        s = model::ModelPrefillTextChunk(
            model_.Get(), &seq, ids.data(), T, count,
            last ? d_prefill_logits_ : nullptr, nullptr, nullptr,
            model::LogitsRows::kLastRow, model::SequenceCompletion::kDeferred);
        cudaError_t copy_error = cudaSuccess;
        if (s.ok() && last)
          copy_error = cudaMemcpyAsync(h_logits.data(), d_prefill_logits_,
                                       static_cast<size_t>(vocab) * 2,
                                       cudaMemcpyDeviceToHost, nullptr);
        model::ModelSequence* sequence = &seq;
        s = FinishHostReadback(copy_error, &gpu_healthy_, {&sequence, 1}, s,
                               router_trace_.get(),
                               &metrics_.execution_steps_total);
        if (s.ok() && !last) save_shared(boundary);
      }
      if (!s.ok()) seq.Fail();
    } else if (scheduled_chunk_prefill) {
      ChunkPrefillReq pr;
      pr.fd = fd;
      pr.control = control.get();
      pr.seq = &seq;
      pr.seq_id = seq_id;
      pr.ids = ids.data();
      pr.len = T;
      pr.h_logits = h_logits.data();
      RequestDiagnostics::Scope wait(diagnostic, Wait::kScheduler);
      SubmitSchedulerRequestAndWait(
          sched_mu_, scheduler_stop_, sched_cv_, pr.cv,
          [&] { chunk_prefill_pending_.push_back(&pr); },
          [&] { return pr.done; });
      prefill_cancelled = pr.cancelled;
      s = pr.ok ? Status()
                : Status::Fail("scheduled chunk prefill failed" +
                               (pr.err.empty() ? std::string()
                                               : std::string(": ") + pr.err));
    } else if (batched_prefill) {
      PrefillReq pr;
      pr.control = control.get();
      pr.fd = fd;
      pr.seq = &seq;
      pr.seq_id = seq_id;
      pr.ids = ids.data();
      pr.len = T;
      pr.h_logits = h_logits.data();
      RequestDiagnostics::Scope wait(diagnostic, Wait::kScheduler);
      SubmitSchedulerRequestAndWait(
          sched_mu_, scheduler_stop_, sched_cv_, pr.cv,
          [&] {
            pr.pending = true;
            prefill_pending_.push_back(&pr);
          },
          [&] { return pr.done; });
      s = pr.ok ? Status() : Status::Fail("batched prefill failed");
    } else {
      // The request ID outlives this batch. Flush only after the existing final
      // readback boundary; do not log between asynchronous chunk submissions.
      q4t::diagnostic::Batch execution_batch(request_id.c_str(), seq_id, 0, T);
      q4t::diagnostic::Scope execution_prefill("handler_prefill", 0, T);
      auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
      ProgressExecution progress_execution(diagnostic.Work());
      OnException backend_failure([&] { AbortBackendExecution(); });
      {
        q4t::diagnostic::Scope execution_begin("target_begin_sequence", 0, T);
        s = ExecutionStatus();
        if (s.ok())
          s = cancelled() ? Status::Fail("request cancelled")
                          : model::ModelBeginSequence(model_.Get(), &seq,
                                                      nullptr, seq_id);
      }
      int prefill_base = 0;
      int prefill_tokens = std::min(chunk, T);
      if (s.ok()) {
        if (!chunked) {
          if (router_trace_)
            router_trace_->BeginForward(trace::RouteStage::kPrefill, 0, T);
          // Keep full trunk output for MTP, but only the final logits row.
          // Deferred completion does not make this a pure CPU submission span:
          // model internals may still wait for metadata, PLE or MoE readbacks.
          q4t::diagnostic::Scope execution_submit("target_prefill_call", 0, T);
          s = model::ModelPrefill(model_.Get(), &seq, ids.data(), T,
                                  d_prefill_logits_, nullptr,
                                  mtp_loaded_ ? d_trunk_full : nullptr, vptr,
                                  seq_id, model::LogitsRows::kLastRow,
                                  model::SequenceCompletion::kDeferred);
        } else {
          // Text chunks share the sequence's slot, position and PLE history.
          // Intermediate chunks only produce trunk/state; the final chunk
          // writes one logits row at the start of the shared buffer.
          while (s.ok() && seq.position < T) {
            if (cancelled()) {
              prefill_cancelled = true;
              s = Status::Fail("client disconnected during prefill");
              break;
            }
            const int base = seq.position;
            const int c = std::min(chunk, T - base);
            const bool last = (base + c == T);
            prefill_base = base;
            prefill_tokens = c;
            q4t::diagnostic::Scope execution_chunk("target_prefill_chunk", base,
                                                   c);
            if (router_trace_)
              router_trace_->BeginForward(trace::RouteStage::kPrefill, base, c);
            {
              q4t::diagnostic::Scope execution_submit("target_prefill_call",
                                                      base, c);
              s = model::ModelPrefillTextChunk(
                  model_.Get(), &seq, ids.data(), T, c,
                  last ? d_prefill_logits_ : nullptr, nullptr,
                  d_trunk_full
                      ? d_trunk_full + static_cast<size_t>(base) * trunk_hc_dim
                      : nullptr,
                  model::LogitsRows::kLastRow,
                  model::SequenceCompletion::kDeferred);
            }
            if (!last) {
              model::ModelSequence* sequence = &seq;
              {
                // This includes the existing stream wait and sequence commit.
                q4t::diagnostic::Scope execution_wait(
                    "prefill_chunk_readback_wait_commit", base, c);
                s = FinishHostReadback(cudaSuccess, &gpu_healthy_,
                                       {&sequence, 1}, s, router_trace_.get(),
                                       &metrics_.execution_steps_total);
              }
              // Diagnostic: memory trajectory across chunked prefill (the
              // 204800-tier OOM investigation). Never touches compute.
              {
                q4t::diagnostic::Scope execution_memory(
                    "prefill_chunk_memory_snapshot", base, c);
                LogMemorySnapshot("prefill_chunk",
                                  "seq" + std::to_string(seq_id) + "@pos" +
                                      std::to_string(seq.position));
              }
            }
            if (last || !s.ok()) break;
          }
        }
      }
      cudaError_t copy_error = cudaSuccess;
      if (s.ok()) {
        q4t::diagnostic::Scope execution_copy("prefill_final_d2h_submit",
                                              prefill_base, prefill_tokens);
        copy_error = cudaMemcpyAsync(h_logits.data(), d_prefill_logits_,
                                     static_cast<size_t>(vocab) * 2,
                                     cudaMemcpyDeviceToHost, nullptr);
      }
      model::ModelSequence* sequence = &seq;
      // Drain and commit while holding model_mu_, also on failed submission.
      const auto pending =
          seq.HasPending()
              ? std::span<model::ModelSequence* const>(&sequence, 1)
              : std::span<model::ModelSequence* const>();
      {
        q4t::diagnostic::Scope execution_wait(
            "prefill_final_readback_wait_commit", prefill_base, prefill_tokens);
        s = FinishHostReadback(
            copy_error, &gpu_healthy_, pending, s, router_trace_.get(),
            pending.empty() ? nullptr : &metrics_.execution_steps_total);
      }
      if (!s.ok()) seq.Fail();
    }
    if ((prefill_cancelled || cancelled()) &&
        gpu_healthy_.load(std::memory_order_relaxed)) {
      // Scheduler chunks have drained before notification; the inline path
      // drains above. The request thread still owns fd and all request buffers.
      std::fprintf(stderr,
                   "[q4t] prefill cancelled seq=%d position=%d total=%d\n",
                   seq_id, seq.position, T);
      count_abort();
      model::ModelEndSequence(&seq);
      cleanup();
      send_cancel_error("request cancelled during prefill");
      return;
    }
    if (!s.ok()) {
      model::ModelEndSequence(&seq);
      cleanup();
      if (RejectUnhealthy(fd)) return;
      SendError(fd, 500, "prefill failed: " + s.message());
      return;
    }

    diagnostic.Stage("decode");
    if (completion && completion_plan.echo && stream &&
        !completion_prompt->echo_text.empty()) {
      if (!write_stream(CompletionChunk(
              id, model_field, created, choice_index,
              completion_prompt->echo_text, "",
              completion_plan.logprobs >= 0 ? prompt_probabilities.Json()
                                            : "null",
              include_usage, completion_plan.include_obfuscation)))
        return;
    }
    // 3. Decode loop under the per-choice selection contract.
    detail::Utf8Stream utf8;
    Status response_status;

    const auto write_deltas = [&](const std::vector<ResponseDelta>& deltas) {
      for (const auto& delta : deltas) {
        std::string logprobs = "null";
        if (visible_logprobs) {
          response_status = token_logprobs.Emit(delta, &logprobs);
          if (!response_status) return false;
        }
        std::string response_chunk;
        if (responses) {
          if (compaction_step || moderation_step) {
            response_status =
                delta.kind == ResponseDelta::Kind::kContent
                    ? Status()
                    : Status::Fail("Private executor emitted non-text output");
          } else if (pro_step) {
            if (delta.kind == ResponseDelta::Kind::kTool ||
                delta.text.size() > 1024 * 1024 - pro_notes.size()) {
              response_status = Status::Fail(
                  "Pro deliberation emitted a tool or exceeded note capacity");
            } else {
              auto reasoning = delta;
              reasoning.kind = ResponseDelta::Kind::kReasoning;
              response_status =
                  responses->Append(reasoning, "null", &response_chunk);
              if (response_status) pro_notes += delta.text;
            }
          } else if (summary_step) {
            response_status =
                delta.kind == ResponseDelta::Kind::kContent
                    ? responses->AppendSummary(delta.text, &response_chunk)
                    : Status::Fail("Summary executor emitted non-text output");
          } else {
            response_status =
                responses->Append(delta, logprobs, &response_chunk);
          }
          if (!response_status) return false;
        }
        if (!stream || compaction_step || moderation_step ||
            (responses && responses->OutputHeld()))
          continue;
        detail::ServeFaultPoint("handler.before_content");
        const auto chunk =
            responses ? response_chunk
            : completion
                ? CompletionChunk(id, model_field, created, choice_index,
                                  delta.text, "", logprobs, include_usage,
                                  completion_plan.include_obfuscation)
                : detail::SseDeltaChunk(id, model_field, delta.ToJson(), "",
                                        choice_index, created, include_usage,
                                        logprobs);
        if (!write_stream(chunk))
          return false;
        if (!first_delta_sent) {
          first_delta_sent = true;
          metrics_.ttft_seconds.Observe(
              std::chrono::duration<double>(std::chrono::steady_clock::now() -
                                            t_arrive)
                  .count());
        }
      }
      if (visible_logprobs)
        token_logprobs.DiscardBefore(response_parser.ConsumedBytes());
      return true;
    };
    const auto write_content = [&](const std::string& piece) {
      if (!response_status) return false;
      std::vector<ResponseDelta> deltas;
      response_status = response_parser.Append(piece, &deltas);
      return response_status.ok() && write_deltas(deltas);
    };
    // Budget, probability and byte provenance all follow actual accepted
    // tokens. Model history is never reconstructed from parsed response text.
    const auto consume_token = [&](int32_t token) {
      if (completion && is_stop_token(token)) return true;
      if (!responses && !completion && !stream && !thinking_budget.Tracking() &&
          !text_stop.Enabled() && !sampling.logprobs && !prediction_enabled)
        return true;
      std::string piece;
      {
        auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer);
        response_status =
            tok_->Decode({static_cast<uint32_t>(token)}, !completion, &piece);
        if (response_status && visible_logprobs)
          response_status = token_logprobs.Push(
              selected_sample, piece, [&](int32_t id, std::string* bytes) {
                return tok_->Decode({static_cast<uint32_t>(id)}, false, bytes);
              });
      }
      if (response_status && thinking_budget.Tracking())
        response_status =
            thinking_budget.Observe(token, piece, is_stop_token(token));
      if (!response_status) return false;
      if (responses || completion || stream || text_stop.Enabled() ||
          sampling.logprobs || prediction_enabled) {
        std::vector<detail::Utf8Span> spans;
        const auto normalized =
            utf8.Append(piece, visible_logprobs ? &spans : nullptr);
        if (visible_logprobs) token_logprobs.Map(spans);
        if (!write_content(text_stop.Append(normalized))) return false;
      }
      return !text_stop.Matched();
    };

    std::vector<int32_t> generated;
    int next_token = -1;
    std::string finish_reason = max_tokens == 0 && !prewarm ? "length" : "stop";
    bool generation_failed = false;
    int stop_mtp_steps = 0;
    int stop_mtp_tokens = 0;
    int stop_mtp_discarded = 0;
    int budget_mtp_steps = 0;
    int budget_mtp_tokens = 0;
    bool budget_mtp_fallback = false;

    const auto select_token = [&](const uint16_t* logits) {
      const auto forced = thinking_budget.ForcedToken();
      if (!plain_greedy) {
        auto allowed = sampling_allowed;
        if (forced < 0 && thinking_budget.Enabled() &&
            thinking_budget.Consumed() >= reasoning_options.token_budget) {
          auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer, cancelled);
          if (!lock.owns_lock()) {
            response_status = Status::Fail("sampling cancelled");
            return -1;
          }
          for (int32_t token = 0; token < planning_config.token_vocab;
               ++token) {
            if (!allowed[token] || is_stop_token(token)) continue;
            if (token % 256 == 0 && cancelled()) {
              response_status = Status::Fail("sampling cancelled");
              return -1;
            }
            std::string piece;
            response_status =
                tok_->Decode({static_cast<uint32_t>(token)}, true, &piece);
            if (!response_status) return -1;
            allowed[token] = thinking_budget.Allows(piece);
          }
        }
        const std::span<const uint16_t> row(logits, vocab);
        int32_t token = -1;
        if (structured) {
          RequestDiagnostics::Scope waiting(diagnostic, Wait::kGrammar);
          response_status = structured->Select(row, &token, forced, &sampler,
                                               &selected_sample, allowed);
        } else {
          response_status =
              sampler.Select(row, &selected_sample, allowed, forced);
          token = selected_sample.token;
        }
        return response_status ? token : -1;
      }
      if (!structured) {
        if (!thinking_budget.Enabled() ||
            thinking_budget.Consumed() < reasoning_options.token_budget)
          return argmax(logits);
        // The remaining plain tail has per-request selection constraints. Its
        // logits belong to the state *before* this next token is consumed.
        int32_t best_token = -1;
        float best = -std::numeric_limits<float>::infinity();
        for (int32_t token = 0; token < vocab; ++token) {
          if (forced >= 0 && token != forced) continue;
          const uint32_t bits = static_cast<uint32_t>(logits[token]) << 16;
          float value;
          std::memcpy(&value, &bits, sizeof(value));
          if (!std::isfinite(value) || value <= best) continue;
          if (forced < 0 && !is_stop_token(token)) {
            if (static_cast<uint32_t>(token) >= tok_->VocabSize()) continue;
            std::string piece;
            {
              auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer);
              response_status =
                  tok_->Decode({static_cast<uint32_t>(token)}, true, &piece);
            }
            if (!response_status) return -1;
            if (!thinking_budget.Allows(piece)) continue;
          }
          best_token = token;
          best = value;
        }
        if (best_token < 0)
          response_status =
              Status::Fail("no finite thinking-budget-allowed token");
        return best_token;
      }
      int32_t token = -1;
      RequestDiagnostics::Scope waiting(diagnostic, Wait::kGrammar);
      response_status = structured->Select({logits, static_cast<size_t>(vocab)},
                                           &token, forced);
      return token;
    };

    // Every prefill path reads its selected final row under model_mu_ and
    // checks stream completion before publishing h_logits to this thread.
    if (max_tokens > 0) next_token = select_token(h_logits.data());
    if (choice_index == 0)
      metrics_.target_ready_seconds.Observe(
          std::chrono::duration<double>(std::chrono::steady_clock::now() -
                                        t_arrive)
              .count());
    err_guard.ok = true;  // generation has started; not a pre-generation error

    // MTP init (mirrors the CLI --mtp path): fresh draft KV, bonus token b =
    // t_P (the first decode token), draft-extend over the prompt to build the
    // draft KV[0..P-1] and seed the first speculative step (d0 + g). On any
    // failure fall back to plain decode (the main seq is still usable).
    //
    // Stage 2c (4b): the MTP path is per-sequence safe AND batched. The draft
    // KV is pooled per seq_id (mcfg.max_seq, Stage 1) and each request owns a
    // per-request rolling trunk (d_mtp_g). MTP init (per-seq reset + draft
    // extend) runs on this thread under model_mu_; the speculative STEPS are
    // registered with the central scheduler, which batches all concurrent MTP
    // requests into ONE MtpSpeculativeStepMulti (batched draft loop +
    // ModelVerifyMulti + batched extend, weights read once) — the same code
    // path a single MTP request takes (B=1). This thread advances the main seq
    // over the accepted prefix (the multi step does not touch seqs[b]). MTP is
    // enabled for BOTH one-shot and chunked prefill, as long as the full-prompt
    // trunk buffer was allocated (d_trunk_full != nullptr). The chunked path
    // accumulates the trunk across prefill chunks and runs the draft-extend in
    // chunks (MtpDraftExtend's chunked path). The draft KV/indexer is allocated
    // at MTP load time (not per-request), so the only per-request cost is the
    // trunk buffer; if its allocation failed, d_trunk_full is null and we fall
    // back to plain decode. MTP is precision-safe: the main model verifies
    // every draft token under the documented T>1 near-tie contract; it need not
    // be bit-identical to the T=1 arithmetic path.
    bool use_mtp = request_mtp && max_tokens > 0 && d_trunk_full != nullptr &&
                   thinking_budget.CanSpeculate(mtp_k_ + 1);
    int32_t mtp_b = stream_draft_ready ? next_token : -1;
    int32_t mtp_d0 = stream_draft_ready ? stream_d0 : -1;
    if (use_mtp && !stream_mtp) {
      const auto allocated = allocate_buffer(
          d_mtp_g, static_cast<size_t>(mtp_.cfg.hc) * mtp_.cfg.hs * 2,
          "request.draft_trunk");
      if (!allocated.ok() && !allocated.exhausted()) {
        allocation_error(allocated);
        return;
      }
      if (allocated.exhausted()) {
        std::fprintf(stderr,
                     "[q4t] MTP trunk allocation exhausted; plain decode\n");
        use_mtp = false;
      }
    }
    bool mtp_prepare_failed = false;
    if (use_mtp && !stream_mtp) {
      {
        q4t::diagnostic::Batch execution_batch(request_id.c_str(), seq_id, 0,
                                               T);
        q4t::diagnostic::Scope execution_init("handler_mtp_init", 0, T);
        auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
        ProgressExecution progress_execution(ProgressWork::kDraft);
        OnException backend_failure([&] { AbortBackendExecution(); });
        // Another request may have failed PLE while this one waited. No draft
        // work has begun here; do not reinterpret that fault as a CUDA failure.
        if (RejectUnhealthy(fd)) return;
        {
          q4t::diagnostic::Scope execution_reset("prompt_draft_reset", 0, T);
          s = mtp::MtpResetState(mtp_, nullptr, seq_id);
        }
        if (s.ok()) {
          mtp_b = next_token;
          // EAGLE shift: shifted_ids[p] = t_{p+1}, with t_P := b at the tail.
          std::vector<int32_t> shifted(T);
          for (int i = 0; i < T - 1; ++i) shifted[i] = ids[i + 1];
          shifted[T - 1] = mtp_b;
          std::vector<int> pos(T);
          for (int i = 0; i < T; ++i) pos[i] = i;
          {
            // Aggregate call: includes internal submission, existing waits and
            // cleanup. Its child spans must not be added to this parent span.
            q4t::diagnostic::Scope execution_extend("prompt_draft_extend_call",
                                                    0, T);
            s = mtp::MtpDraftExtend(mtp_, shifted.data(), d_trunk_full,
                                    pos.data(), T, &mtp_d0, d_mtp_g, nullptr,
                                    seq_id);
            if (s.ok()) {
              metrics_.execution_steps_total.fetch_add(
                  1, std::memory_order_relaxed);
              ProgressExecution::Completed();
            }
          }
          if (s.ok()) {
            q4t::diagnostic::Scope execution_reserve("prompt_verify_reserve", 0,
                                                     T);
            s = model::ModelReserveVerifyCheckpoints(model_.Get(), mtp_k_);
            if (s.ok()) s = mtp::MtpReserveScratch(mtp_, mtp_k_ + 1);
          }
        }
        if (s.ok() &&
            detail::DeviceMemoryFault("request.mtp_prepare",
                                      request_id.c_str()) != cudaSuccess)
          s = Status::Fail("validation MTP preparation failure");
        if (!s.ok()) {
          std::fprintf(stderr,
                       "[q4t] MTP preparation failed (%s); stopping serve\n",
                       s.message().c_str());
          // Nested preparation exposes only an untyped Status. It does not
          // prove a recoverable allocation failure or safe reuse of submitted
          // state.
          AbortBackendExecution();  // model_mu_ is held; drain before
                                    // unwinding.
          mtp_prepare_failed = true;
        }
      }
    }
    if (mtp_prepare_failed) {
      err_guard.ok = false;
      diagnostic.Outcome("error", "mtp_prepare_failed");
      SendError(fd, 503, "MTP preparation failed; restart server", {},
                "gpu_unhealthy");
      return;
    }
    // The prompt trunk is only needed for the draft-extend above; free it now
    // so decoding requests do not hold it (bounds concurrent memory).
    if (d_trunk_full) {
      q4t::diagnostic::Batch execution_batch(request_id.c_str(), seq_id, 0, T);
      q4t::diagnostic::Scope execution_free("prompt_trunk_free", 0, T);
      release_buffer(d_trunk_full, "request.prompt_trunk_free");
      if (!buffers_ok) {
        err_guard.ok = false;
        diagnostic.Outcome("error", "device_memory_failure");
        SendError(fd, 503, "device buffer release failed; restart server", {},
                  "gpu_unhealthy");
        return;
      }
    }
    // Stage 2c (4b): the speculative steps are driven by the central scheduler,
    // which batches all concurrent MTP requests into ONE
    // MtpSpeculativeStepMulti (weights read once). This thread registers each
    // step's input (mtp_b/d0/g), blocks on the request's cv, and is woken with
    // the step's output. It owns the state-machine advance (position/history)
    // over the accepted prefix. Fallback: if the scheduler is unavailable or
    // the pool is full, decode plainly (the main seq is still usable).
    ActiveRequest mtp_ar;
    mtp::SuffixDrafter suffix;
    struct RequestClasses {
      ChatServer* server;
      uint64_t decodes = 0, lookups = 0, allocations = 0;
      size_t bytes = 0;
    } request_classes{this};
    std::array<uint64_t, 4> suffix_accepted{};
    std::array<uint64_t, 4> neural_accepted{};
    bool mtp_sched = false;
    ScopeExit mtp_registration([&] {
      if (mtp_sched) ReleaseActiveRequest(&mtp_ar);
    });
    if (use_mtp) {
      const std::lock_guard<std::mutex> lock(sched_mu_);
      mtp_sched = scheduler_active_ && !scheduler_stop_ && d_sched_logits_ &&
                  static_cast<int>(active_.size()) < max_seq_;
      if (mtp_sched) {
        mtp_ar.is_mtp = true;
        mtp_ar.complete_terminal_draft = session.cache != nullptr;
        mtp_ar.session_last_trunk = d_session_trunk;
        mtp_ar.seq = &seq;
        mtp_ar.control = control.get();
        mtp_ar.fd = fd;
        mtp_ar.mtp_g = d_mtp_g;

        active_.push_back(&mtp_ar);
        detail::ServeFaultPoint("handler.after_active");
      } else {
        use_mtp = false;
      }
    }
    if (use_mtp && mtp_suffix_) {
      const Status suffix_status = suffix.Init(
          seq.history, model_.Get().cfg.vocab, request_capacity, {},
          {&request_classes,
           [](void* context, std::span<const int32_t> ids, bool* numeric) {
             auto& local = *static_cast<RequestClasses*>(context);
             auto* server = local.server;
             const std::lock_guard<std::mutex> lock(server->tok_mu_);
             auto& cache = server->suffix_token_cache_;
             const auto before = cache.Counters();
             const Status status = cache.Check(ids, numeric);
             const auto& after = cache.Counters();
             local.decodes += after.decodes - before.decodes;
             local.lookups += after.lookups - before.lookups;
             local.allocations += after.allocations - before.allocations;
             local.bytes = cache.MemoryBytes();
             return status;
           }});
      if (!suffix_status.ok())
        std::fprintf(stderr, "[suffix] request=%s disabled=%s\n",
                     request_id.c_str(), suffix_status.message().c_str());
    }
    if (use_mtp) {
      int accepted_histogram[17] = {};
      bool done = false;
      while (!done && static_cast<int>(generated.size()) < max_tokens) {
        if (cancelled()) break;
        if (!mtp_sched) break;
        if (is_stop_token(mtp_b)) {
          generated.push_back(mtp_b);
          observe_completion(completed_tokens() + generated.size());
          finish_reason = "stop";
          break;
        }
        const int remaining = max_tokens - static_cast<int>(generated.size());
        if (!thinking_budget.CanSpeculate(mtp_k_ + 1)) {
          // Reserve one admitted pending token before switching to host logits.
          // No speculative work or KV for this next step has been submitted.
          next_token = mtp_b;
          use_mtp = false;
          budget_mtp_fallback = true;
          break;
        }
        if (!MtpGenerationStepFits(seq.position, request_capacity,
                                   model_.Get().cfg.max_prefill, mtp_k_,
                                   remaining)) {
          // No speculative work has started. Finish with ordinary decode,
          // preserving the pending token and the already emitted prefix.
          next_token = mtp_b;
          use_mtp = false;
          break;
        }
        // Register this step's input with the scheduler.
        mtp_ar.mtp_b = mtp_b;
        mtp_ar.mtp_d0 = mtp_d0;
        mtp_ar.mtp_suffix_ready = false;
        if (suffix.Active()) {
          const auto proposal = suffix.ProposeAfter(mtp_b, mtp_d0);
          mtp_ar.mtp_suffix_ready = proposal.ready;
          mtp_ar.mtp_suffix_tokens = proposal.tokens;
        }
        {
          RequestDiagnostics::Scope wait(diagnostic, Wait::kScheduler);
          {
            const std::lock_guard<std::mutex> lock(sched_mu_);
            mtp_ar.mtp_accepted_count = 0;
            mtp_ar.mtp_next_b = -1;
            mtp_ar.mtp_next_d0 = -1;
            mtp_ar.pending = !scheduler_stop_ && !mtp_ar.retired;
            mtp_ar.done = !mtp_ar.pending;
            if (mtp_ar.done) seq.Fail();
          }
          sched_cv_.notify_one();
          // Block until the scheduler's batched step yields this step's output.
          {
            std::unique_lock<std::mutex> lock(sched_mu_);
            mtp_ar.cv.wait(lock, [&mtp_ar] { return mtp_ar.done; });
          }
        }
        if (mtp_ar.mtp_accepted_count <= 0 ||
            mtp_ar.mtp_accepted_count > mtp_k_ + 1) {
          // Scheduler failed the step or is shutting down.
          generation_failed = true;
          break;
        }
        ++accepted_histogram[mtp_ar.mtp_accepted_count - 1];
        if (text_stop.Enabled()) {
          ++stop_mtp_steps;
          stop_mtp_tokens += mtp_ar.mtp_accepted_count;
        }
        if (thinking_budget.Enabled()) {
          ++budget_mtp_steps;
          budget_mtp_tokens += mtp_ar.mtp_accepted_count;
        }
        if (mtp_suffix_) {
          auto& histogram =
              mtp_ar.mtp_suffix_ready ? suffix_accepted : neural_accepted;
          ++histogram[mtp_ar.mtp_accepted_count - 1];
        }
        for (int i = 0; i < mtp_ar.mtp_accepted_count &&
                        static_cast<int>(generated.size()) < max_tokens;
             ++i) {
          const int32_t tok_id = mtp_ar.mtp_accepted[i];
          generated.push_back(tok_id);
          observe_completion(completed_tokens() + generated.size());
          if (!consume_token(tok_id)) {
            if (text_stop.Matched())
              stop_mtp_discarded = mtp_ar.mtp_accepted_count - i - 1;
            done = true;
            finish_reason = "stop";
            break;
          }
          if (is_stop_token(tok_id)) {
            done = true;
            break;
          }
        }
        // Advance the main seq over the accepted prefix [b, d_0..d_{a-1}] (the
        // multi step does not touch seqs[b]).
        seq.position += mtp_ar.mtp_accepted_count;
        for (int i = 0; i < mtp_ar.mtp_accepted_count; ++i)
          seq.history.push_back(mtp_ar.mtp_accepted[i]);
        if (suffix.Active()) {
          const Status suffix_status =
              suffix.Append({mtp_ar.mtp_accepted,
                             static_cast<size_t>(mtp_ar.mtp_accepted_count)});
          if (!suffix_status.ok())
            std::fprintf(stderr, "[suffix] request=%s disabled=%s\n",
                         request_id.c_str(), suffix_status.message().c_str());
        }
        // Stop/EOS/parser failure wins over a coincident length bound. KV was
        // committed for the entire accepted batch; keep that state intact and
        // retire it. Stop+cache is rejected before model work, so the invisible
        // speculative suffix cannot become a reusable response snapshot.
        if (done) break;
        if (static_cast<int>(generated.size()) >= max_tokens) {
          finish_reason = "length";
          break;
        }
        if (seq.position >= request_capacity) {
          finish_reason = "length";
          break;  // cannot decode further without exceeding the KV cache
        }
        mtp_b = mtp_ar.mtp_next_b;
        mtp_d0 = mtp_ar.mtp_next_d0;
        // d_mtp_g was overwritten in place with the next step's trunk.
      }
      flockfile(stderr);
      std::fprintf(stderr,
                   "[mtp-accept] request=%s draft_counts=", request_id.c_str());
      for (int i = 0; i <= mtp_k_; ++i)
        std::fprintf(stderr, "%s%d", i ? "," : "", accepted_histogram[i]);
      std::fprintf(stderr, "\n");
      funlockfile(stderr);
    }
    mtp_registration.Run();
    // No scheduler owns this row now. Capture the complete target/shifted-draft
    // boundary before any ordinary-decode tail; publication waits for
    // successful HTTP completion. A thinking-budget tail can be longer than the
    // ordinary capacity tail; only this complete earlier MTP boundary is
    // reusable.
    if (session.cache && mtp_sched && !generation_failed &&
        response_status.ok() && !cancelled() &&
        seq.stage == model::ModelSequence::Stage::kDecode &&
        !seq.HasPending() && seq.position >= SessionCache::kMinPrefix &&
        seq.position < request_capacity) {
      const auto begin = std::chrono::steady_clock::now();
      SessionSaveTiming timing(request_id.c_str(), "private");
      std::unique_lock<std::mutex> lock;
      {
        SessionSaveTiming::Scope span(&timing, SessionSaveTiming::kModelLock);
        lock = diagnostic.Lock(model_mu_, Wait::kModel);
      }
      RequestDiagnostics::Scope waiting(diagnostic, Wait::kCache);
      OnException backend_failure([&] { AbortBackendExecution(); });
      std::vector<SessionRange> ranges;
      Status saved;
      {
        SessionSaveTiming::Scope span(&timing, SessionSaveTiming::kRanges);
        saved = SessionStateRanges(model_.Get(), mtp_, seq_id, seq.position,
                                   d_mtp_g, d_session_trunk, &ranges);
      }
      const bool has_step = mtp_ar.mtp_accepted_count > 0;
      const int32_t pending = has_step ? mtp_ar.mtp_next_b : mtp_b;
      const int32_t last_d0 = has_step ? mtp_ar.mtp_next_d0 : mtp_d0;
      if (saved.ok() && pending >= 0 && last_d0 >= 0 &&
          model_.Get().rope_delta[seq_id] == 0) {
        std::vector<int32_t> prefix(seq.history);
        prefix.push_back(pending);
        saved = session_cache_.Save(session.key, prefix, ranges, session_copy,
                                    cancelled, last_d0, &timing);
        if (saved.ok()) detail::ServeSnapshotTokens(request_id.c_str(), prefix);
      } else if (saved.ok()) {
        saved = Status::Fail("incomplete session boundary");
      }
      timing.Report(saved.ok());
      if (session_gpu_error) {
        generation_failed = true;
        use_mtp = true;  // Do not enter a plain tail after a device failure.
        seq.Fail();
      }
      std::fprintf(stderr,
                   "[session-cache] request=%s event=save prefix=%d "
                   "ms=%.3f disk_bytes=%zu error=%s\n",
                   request_id.c_str(), seq.position,
                   std::chrono::duration<double, std::milli>(
                       std::chrono::steady_clock::now() - begin)
                       .count(),
                   session_cache_.Bytes(),
                   saved.ok() ? "none" : saved.message().c_str());
    }
    if (mtp_suffix_) {
      const size_t indexed = suffix.Size();
      suffix.Reset();
      const auto& stats = suffix.Counters();
      flockfile(stderr);
      std::fprintf(
          stderr,
          "[suffix] request=%s queries=%llu hits=%llu "
          "candidates=%llu compared=%llu matched=%llu "
          "no_match=%llu incomplete=%llu d0_mismatch=%llu "
          "invalid=%llu exhausted=%llu indexed=%zu peak_bytes=%zu "
          "object_bytes=%zu init_ns=%llu append_ns=%llu "
          "query_ns=%llu release_ns=%llu numeric_checks=%llu "
          "classified_tokens=%llu numeric_rejected=%llu "
          "numeric_ns=%llu short_rejected=%llu classify_failed=%llu "
          "cache_decodes=%llu cache_lookups=%llu "
          "cache_allocations=%llu cache_bytes=%zu accepted=",
          request_id.c_str(), static_cast<unsigned long long>(stats.queries),
          static_cast<unsigned long long>(stats.hits),
          static_cast<unsigned long long>(stats.candidates_examined),
          static_cast<unsigned long long>(stats.compared_tokens),
          static_cast<unsigned long long>(stats.matched_tokens),
          static_cast<unsigned long long>(stats.no_match),
          static_cast<unsigned long long>(stats.incomplete),
          static_cast<unsigned long long>(stats.first_token_mismatch),
          static_cast<unsigned long long>(stats.invalid_tokens),
          static_cast<unsigned long long>(stats.capacity_exhaustions), indexed,
          stats.peak_memory_bytes, sizeof(suffix),
          static_cast<unsigned long long>(stats.init_ns),
          static_cast<unsigned long long>(stats.append_ns),
          static_cast<unsigned long long>(stats.query_ns),
          static_cast<unsigned long long>(stats.release_ns),
          static_cast<unsigned long long>(stats.numeric_checks),
          static_cast<unsigned long long>(stats.classified_tokens),
          static_cast<unsigned long long>(stats.numeric_rejected),
          // numeric_ns is already included in query_ns.
          static_cast<unsigned long long>(stats.numeric_ns),
          static_cast<unsigned long long>(stats.short_rejected),
          static_cast<unsigned long long>(stats.classification_failures),
          static_cast<unsigned long long>(request_classes.decodes),
          static_cast<unsigned long long>(request_classes.lookups),
          static_cast<unsigned long long>(request_classes.allocations),
          request_classes.bytes);
      for (size_t i = 0; i < suffix_accepted.size(); ++i)
        std::fprintf(stderr, "%s%llu", i ? "," : "",
                     static_cast<unsigned long long>(suffix_accepted[i]));
      std::fprintf(stderr, " neural_accepted=");
      for (size_t i = 0; i < neural_accepted.size(); ++i)
        std::fprintf(stderr, "%s%llu", i ? "," : "",
                     static_cast<unsigned long long>(neural_accepted[i]));
      std::fprintf(stderr, "\n");
      funlockfile(stderr);
    }
    if (!use_mtp) {
      // B2b continuous batching: this request's decode steps are driven by the
      // central scheduler, which packs the current token of EVERY active
      // plain-decode request into ONE ModelDecodeBatchMulti (weights read once
      // for all B tokens). This thread only emits tokens and advances the
      // per-sequence state machine; the GPU forward is the scheduler's job.
      //
      // Fallback: if the scheduler is unavailable (buffer alloc failed) or the
      // active pool is full, this request decodes on its own via
      // ModelDecodeStepSeq (the B1 single-sequence path).
      bool use_sched = false;
      ActiveRequest ar;
      ScopeExit active_registration([&] {
        if (use_sched) ReleaseActiveRequest(&ar);
      });
      {
        const std::lock_guard<std::mutex> lock(sched_mu_);
        // The current packed scheduler argmax has no request-specific mask.
        // Constrained requests must stay on the host-logit fallback until a
        // separately accepted scheduler/MTP mask implementation exists.
        use_sched = plain_greedy && !prediction_enabled && !structured &&
                    !thinking_budget.Enabled() && scheduler_active_ &&
                    !scheduler_stop_ && d_sched_logits_ &&
                    static_cast<int>(active_.size()) < max_seq_;
        if (use_sched) {
          ar.seq = &seq;
          ar.control = control.get();
          ar.fd = fd;
          active_.push_back(&ar);
          detail::ServeFaultPoint("handler.after_active");
        }
      }
      for (int step = static_cast<int>(generated.size()); step < max_tokens;
           ++step) {
        if (cancelled() || !response_status) break;
        // Prediction tokens not consumed in the completion are charged as
        // rejected output, including an unused suffix at EOS or truncation.
        // Matching one next token adds no net charge: it replaces a reserved
        // prediction token. This permits a fully predicted max_tokens reply.
        const bool prediction_content =
            !response_parser.Thinking() && next_token >= 0 &&
            next_token < static_cast<int32_t>(tok_->BaseVocabSize());
        const uint64_t prediction_credit =
            prediction_content && prediction.Matches(next_token) ? 1 : 0;
        if (prediction_enabled &&
            prediction.Completion(generated.size() + 1) - prediction_credit >
                static_cast<uint64_t>(prompt_budget.available_output)) {
          finish_reason = "length";
          break;
        }
        if (!plain_greedy) response_status = sampler.Observe(next_token);
        if (!response_status) break;
        generated.push_back(next_token);
        if (completion && sampling.logprobs) {
          completion_score += selected_sample.raw_logprob;
          ++completion_scored_tokens;
        }
        prediction.Consume(next_token, prediction_content);
        observe_completion(completed_tokens() +
                           prediction.Completion(generated.size()));
        if (is_stop_token(next_token)) {
          finish_reason = "stop";
          break;
        }
        const int32_t tok_id = next_token;
        if (!consume_token(tok_id)) {
          finish_reason = "stop";
          break;
        }
        // The current token is already emitted (or buffered for non-streaming).
        // Do not prepare another token when this request cannot consume it.
        if (step == max_tokens - 1 ||
            seq.position + (prediction_logits_ready ? 0 : 1) >=
                request_capacity) {
          finish_reason = "length";
          break;
        }
        if (prediction_logits_ready) {
          // The previous verify already consumed this token. Its second
          // logits row belongs to this exact sequence prefix, copied to host
          // before another request could reuse the shared model scratch.
          prediction_logits_ready = false;
          next_token = select_token(h_prediction_logits.data() + vocab);
        } else if (use_sched) {
          // Register this step's token with the scheduler (position + PLE
          // context are read from the per-sequence state BEFORE advancing).
          RequestDiagnostics::Scope wait(diagnostic, Wait::kScheduler);
          ar.token = tok_id;
          {
            const std::lock_guard<std::mutex> lock(sched_mu_);
            ar.next_token = -1;
            ar.pending = !scheduler_stop_ && !ar.retired;
            ar.done = !ar.pending;
            if (ar.done) seq.Fail();
          }
          sched_cv_.notify_one();
          // Block until the scheduler's packed forward yields this step's
          // token.
          {
            std::unique_lock<std::mutex> lock(sched_mu_);
            ar.cv.wait(lock, [&ar] { return ar.done; });
          }
          if (ar.next_token < 0) {
            // Scheduler failed the forward or is shutting down.
            generation_failed = true;
            break;
          }
          next_token = ar.next_token;
        } else {
          // Fallback: single-sequence decode (B1 path).
          auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
          ProgressExecution progress_execution(diagnostic.Work());
          OnException backend_failure([&] { AbortBackendExecution(); });
          const int32_t proposal = prediction.Next();
          const bool verify_prediction =
              prediction_enabled && d_prediction_logits &&
              !response_parser.Thinking() && proposal >= 0 &&
              proposal < static_cast<int32_t>(tok_->BaseVocabSize()) &&
              seq.position + 2 <= request_capacity &&
              model_.Get().cfg.max_prefill >= 2;
          if (verify_prediction) {
            const int32_t inputs[2] = {tok_id, proposal};
            const int position = seq.position;
            const int slot = seq_id;
            s = ExecutionStatus();
            if (s) s = model::ModelReserveVerifyCheckpoints(model_.Get(), 1);
            if (s)
              s = seq.Submit(inputs, model::ModelSequence::Stage::kDecode,
                             request_capacity);
            if (router_trace_)
              router_trace_->BeginForward(trace::RouteStage::kDecode, position,
                                          2);
            if (s)
              s = model::ModelVerifyMulti(model_.Get(), inputs, &position,
                                          &slot, seq.history.data(),
                                          static_cast<int>(seq.history.size()),
                                          1, 2, d_prediction_logits, nullptr);
            cudaError_t copy_error = cudaSuccess;
            if (s)
              copy_error = cudaMemcpyAsync(h_prediction_logits.data(),
                                           d_prediction_logits,
                                           static_cast<size_t>(vocab) * 4,
                                           cudaMemcpyDeviceToHost, nullptr);
            // First wait makes logits readable without committing either
            // pending row. The second boundary checks any rollback before
            // committing only the retained target-verified prefix.
            s = FinishHostReadback(copy_error, &gpu_healthy_, {}, s,
                                   router_trace_.get(),
                                   &metrics_.execution_steps_total);
            bool matched = false;
            if (s) {
              ++prediction_pairs;
              next_token = select_token(h_prediction_logits.data());
              matched = response_status && next_token == proposal;
              if (!matched)
                s = model::ModelRestoreCheckpoint(model_.Get(), 0, nullptr,
                                                  seq_id);
              if (s) s = seq.RetainSubmittedPrefix(matched ? 2 : 1);
            }
            if (seq.HasPending()) {
              model::ModelSequence* sequence = &seq;
              s = FinishHostReadback(cudaSuccess, &gpu_healthy_, {&sequence, 1},
                                     s);
            }
            if (!s) {
              generation_failed = true;
              break;
            }
            prediction_logits_ready = matched;
            prediction_pair_matches += matched;
            continue;
          }
          if (router_trace_)
            router_trace_->BeginForward(trace::RouteStage::kDecode,
                                        seq.position, 1);
          s = ExecutionStatus();
          if (s.ok())
            s = model::ModelDecodeStepSeq(
                model_.Get(), &seq, tok_id, d_prefill_logits_, nullptr, nullptr,
                seq_id, model::SequenceCompletion::kDeferred);
          cudaError_t copy_error = cudaSuccess;
          if (s.ok())
            copy_error = cudaMemcpyAsync(h_logits.data(), d_prefill_logits_,
                                         static_cast<size_t>(vocab) * 2,
                                         cudaMemcpyDeviceToHost, nullptr);
          model::ModelSequence* sequence = &seq;
          s = FinishHostReadback(copy_error, &gpu_healthy_, {&sequence, 1}, s,
                                 router_trace_.get(),
                                 &metrics_.execution_steps_total);
          if (!s.ok()) {
            generation_failed = true;
            break;
          }
          next_token = select_token(h_logits.data());
        }
      }
      active_registration.Run();
    }
    // Ordinary decode has left the scheduler, and all target writes are
    // committed. A later parse, cleanup, cancellation or transport failure
    // keeps this snapshot unpublished through the existing SessionLease.
    if (target_cache && session.cache && !generation_failed &&
        response_status.ok() && !cancelled() &&
        seq.stage == model::ModelSequence::Stage::kDecode &&
        !seq.HasPending() && seq.position >= SessionCache::kMinPrefix &&
        seq.position < request_capacity) {
      auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
      ProgressExecution progress_execution(ProgressWork::kCache);
      OnException backend_failure([&] { AbortBackendExecution(); });
      save_target(&session_cache_, session.key, "private");
      if (session_gpu_error) generation_failed = true;
    }
    trace_output_tokens = generated.size();
    if (prediction_enabled) {
      // Also settles an admitted zero-output length completion, where all
      // provided tokens remain rejected. Queued cancellations never start it.
      observe_completion(completed_tokens() +
                         prediction.Completion(generated.size()));
      std::fprintf(
          stderr,
          "[prediction] request=%s provided=%zu accepted=%llu rejected=%llu "
          "verify_pairs=%llu pair_matches=%llu\n",
          request_id.c_str(), prompt_budget.prediction_tokens.size(),
          static_cast<unsigned long long>(prediction.Accepted()),
          static_cast<unsigned long long>(prediction.Rejected()),
          static_cast<unsigned long long>(prediction_pairs),
          static_cast<unsigned long long>(prediction_pair_matches));
    }
    if (thinking_budget.Enabled()) {
      std::fprintf(
          stderr,
          "[thinking-budget] request=%s limit=%lld consumed=%lld forced=%lld "
          "reasoning_tokens=%lld mtp_steps=%d mtp_tokens=%d fallback=%d\n",
          request_id.c_str(),
          static_cast<long long>(reasoning_options.token_budget),
          static_cast<long long>(thinking_budget.Consumed()),
          static_cast<long long>(thinking_budget.ForcedTokens()),
          static_cast<long long>(thinking_budget.ReasoningTokens()),
          budget_mtp_steps, budget_mtp_tokens, budget_mtp_fallback);
      metrics_.reasoning_budget_tokens_total.fetch_add(
          thinking_budget.Consumed());
      metrics_.reasoning_forced_tokens_total.fetch_add(
          thinking_budget.ForcedTokens());
      if (thinking_budget.ForcedTokens())
        metrics_.reasoning_forced_requests.fetch_add(1);
      if (budget_mtp_fallback) metrics_.reasoning_mtp_fallbacks.fetch_add(1);
    }
    if (generation_failed) seq.Fail();
    model::ModelEndSequence(&seq);
    LogMemorySnapshot("request_end", id);
    if (residency_enabled_) {
      quant::MoEResidency::Stats now, prev;
      model::ResidencyTimingSnapshot tim_now, tp;
      uint64_t interval;
      {
        auto lock = diagnostic.Lock(model_mu_, Wait::kModel);
        now = model::SumResidencyStats(model_.Get());
        tim_now = model::SumResidencyTiming(model_.Get());
        prev = residency_stats_prev_;
        tp = residency_timing_prev_;
        residency_stats_prev_ = now;
        residency_timing_prev_ = tim_now;
        interval = ++residency_interval_;
      }
      // Formatting and IO do not hold model_mu_. Interval IDs retain snapshot
      // order even if concurrent handlers print in a different order.
      std::fprintf(
          stderr,
          "[q4t][residency] id=%s scope=global_interval interval=%llu "
          "finish=%s in=%d out=%zu "
          "loads=%llu load_mb=%.1f misses=%llu hits=%llu "
          "evictions=%llu dmiss=%llu dlook=%llu pmiss=%llu "
          "plook=%llu l2h=%llu l2m=%llu l2ev=%llu nvme_mb=%.1f "
          "mh=%llu mw=%llu msk=%llu ld2h=%llu ld2m=%llu "
          "ld2ev=%llu lp2h=%llu lp2m=%llu lp2ev=%llu "
          "pmr=%llu pme=%llu\n",
          id.c_str(), static_cast<unsigned long long>(interval),
          finish_reason.c_str(), T, generated.size(),
          static_cast<unsigned long long>(now.loads - prev.loads),
          (now.load_bytes - prev.load_bytes) / 1e6,
          static_cast<unsigned long long>(now.misses - prev.misses),
          static_cast<unsigned long long>(now.hits - prev.hits),
          static_cast<unsigned long long>(now.evictions - prev.evictions),
          static_cast<unsigned long long>(now.decode_misses -
                                          prev.decode_misses),
          static_cast<unsigned long long>(now.decode_lookups -
                                          prev.decode_lookups),
          static_cast<unsigned long long>(now.prefill_misses -
                                          prev.prefill_misses),
          static_cast<unsigned long long>(now.prefill_lookups -
                                          prev.prefill_lookups),
          static_cast<unsigned long long>(now.l2_hits - prev.l2_hits),
          static_cast<unsigned long long>(now.l2_misses - prev.l2_misses),
          static_cast<unsigned long long>(now.l2_evictions - prev.l2_evictions),
          (now.nvme_read_bytes - prev.nvme_read_bytes) / 1e6,
          static_cast<unsigned long long>(now.mirror_hits - prev.mirror_hits),
          static_cast<unsigned long long>(now.mirror_writebacks -
                                          prev.mirror_writebacks),
          static_cast<unsigned long long>(now.mirror_skips - prev.mirror_skips),
          static_cast<unsigned long long>(now.l2_decode_hits -
                                          prev.l2_decode_hits),
          static_cast<unsigned long long>(now.l2_decode_misses -
                                          prev.l2_decode_misses),
          static_cast<unsigned long long>(now.l2_decode_evictions -
                                          prev.l2_decode_evictions),
          static_cast<unsigned long long>(now.l2_prefill_hits -
                                          prev.l2_prefill_hits),
          static_cast<unsigned long long>(now.l2_prefill_misses -
                                          prev.l2_prefill_misses),
          static_cast<unsigned long long>(now.l2_prefill_evictions -
                                          prev.l2_prefill_evictions),
          static_cast<unsigned long long>(now.pread_merge_runs -
                                          prev.pread_merge_runs),
          static_cast<unsigned long long>(now.pread_merge_experts -
                                          prev.pread_merge_experts));
      // Optional global interval timing; max remains cumulative (upper bound).
      if (tim_now.enabled) {
        auto avg_us = [](uint64_t ns, uint64_t cnt) {
          return cnt ? (static_cast<double>(ns) / cnt) / 1e3 : 0.0;
        };
        std::fprintf(
            stderr,
            "[q4t][residency][timing] id=%s scope=global_interval "
            "interval=%llu "
            "stage_n=%llu stage_avg_us=%.1f stage_max_us=%.1f "
            "pread_n=%llu pread_avg_us=%.1f pread_max_us=%.1f "
            "swz_n=%llu swz_avg_us=%.1f swz_max_us=%.1f "
            "phase1_n=%llu phase1_avg_us=%.1f phase1_max_us=%.1f "
            "dstage_n=%llu dstage_avg_us=%.1f dstage_max_us=%.1f "
            "dpread_n=%llu dpread_avg_us=%.1f dpread_max_us=%.1f "
            "dphase1_n=%llu dphase1_avg_us=%.1f dphase1_max_us=%.1f "
            "d2h_n=%llu d2h_avg_us=%.1f d2h_max_us=%.1f "
            "pmrun_n=%llu pmrun_avg_us=%.1f pmrun_max_us=%.1f\n",
            id.c_str(), static_cast<unsigned long long>(interval),
            static_cast<unsigned long long>(tim_now.stage_count -
                                            tp.stage_count),
            avg_us(tim_now.stage_ns - tp.stage_ns,
                   tim_now.stage_count - tp.stage_count),
            tim_now.stage_max_ns / 1e3,
            static_cast<unsigned long long>(tim_now.pread_count -
                                            tp.pread_count),
            avg_us(tim_now.pread_ns - tp.pread_ns,
                   tim_now.pread_count - tp.pread_count),
            tim_now.pread_max_ns / 1e3,
            static_cast<unsigned long long>(tim_now.swz_count - tp.swz_count),
            avg_us(tim_now.swz_ns - tp.swz_ns,
                   tim_now.swz_count - tp.swz_count),
            tim_now.swz_max_ns / 1e3,
            static_cast<unsigned long long>(tim_now.phase1_count -
                                            tp.phase1_count),
            avg_us(tim_now.phase1_ns - tp.phase1_ns,
                   tim_now.phase1_count - tp.phase1_count),
            tim_now.phase1_max_ns / 1e3,
            static_cast<unsigned long long>(tim_now.dstage_count -
                                            tp.dstage_count),
            avg_us(tim_now.dstage_ns - tp.dstage_ns,
                   tim_now.dstage_count - tp.dstage_count),
            tim_now.dstage_max_ns / 1e3,
            static_cast<unsigned long long>(tim_now.dpread_count -
                                            tp.dpread_count),
            avg_us(tim_now.dpread_ns - tp.dpread_ns,
                   tim_now.dpread_count - tp.dpread_count),
            tim_now.dpread_max_ns / 1e3,
            static_cast<unsigned long long>(tim_now.dphase1_count -
                                            tp.dphase1_count),
            avg_us(tim_now.dphase1_ns - tp.dphase1_ns,
                   tim_now.dphase1_count - tp.dphase1_count),
            tim_now.dphase1_max_ns / 1e3,
            static_cast<unsigned long long>(tim_now.d2h_count - tp.d2h_count),
            avg_us(tim_now.d2h_ns - tp.d2h_ns,
                   tim_now.d2h_count - tp.d2h_count),
            tim_now.d2h_max_ns / 1e3,
            static_cast<unsigned long long>(tim_now.pread_merge_count -
                                            tp.pread_merge_count),
            avg_us(tim_now.pread_merge_ns - tp.pread_merge_ns,
                   tim_now.pread_merge_count - tp.pread_merge_count),
            tim_now.pread_merge_max_ns / 1e3);
      }
    }

    if (completion ? choice_index % completion_plan.best_of == 0
                   : responses || choice_index == 0)
      metrics_.prompt_tokens_total.fetch_add(static_cast<uint64_t>(T),
                                             std::memory_order_relaxed);
    metrics_.generation_tokens_total.fetch_add(generated.size(),
                                               std::memory_order_relaxed);
    if (moderation_step) {
      moderation_usage.input_tokens += T;
      moderation_usage.output_tokens += generated.size();
      ++moderation_usage.model_steps;
    }

#ifdef Q4T_SERVE_FAULT_INJECTION
    // Preserve the actual constrained path before validation or a blocked
    // write. The explicit per-request opt-in and bounded dump never exist in
    // production.
    if (structured) detail::ServeGeneratedTokens(request_id.c_str(), generated);
#endif

    // The handler has left every scheduler list and all submitted work is
    // complete. Cancellation remains possible until the response is sent.
    if (cancelled()) {
      count_abort();
      const detail::WriteOptions terminal{std::chrono::steady_clock::now() +
                                          std::chrono::milliseconds(250)};
      if (stream) {
        detail::SendStreamError(fd, response_state, 409, "request cancelled",
                                terminal, control->Reason());
      } else {
        SendError(fd, 409, "request cancelled", terminal, control->Reason());
      }
      cleanup();
      return;
    }
    if (generation_failed) {
      err_guard.ok = false;
      std::fprintf(stderr, "[q4t] generation failed id=%s\n", id.c_str());
      // The HTTP status is already committed for streaming requests. Publish
      // an error terminal, never a normal stop/usage or a successful request.
      const detail::WriteOptions terminal{std::chrono::steady_clock::now() +
                                          std::chrono::milliseconds(250)};
      if (RejectUnhealthy(fd, terminal)) {
        cleanup();
        return;
      }
      if (moderation_step) {
        cleanup();
        if (RejectUnhealthy(fd, terminal)) return;
        if (!review_failed("moderation_generation_failed",
                           "Classification generation failed"))
          return;
        continue;
      }
      if (stream) {
        detail::SendStreamError(fd, response_state, 500, "generation failed",
                                terminal, "generation_failed");
      } else {
        SendError(fd, 500, "generation failed", terminal, "generation_failed");
      }
      cleanup();
      return;
    }
    diagnostic.Stage("response");
    if (prewarm && !cache_plan.writes.empty() &&
        std::max(cache_written_tokens, restored_prefix) <
            cache_plan.writes.back().tokens) {
      err_guard.ok = false;
      SendError(fd, 503, "Prompt cache prewarm did not persist its prefix", {},
                "prompt_cache_unavailable", "prompt_cache_options.prewarm");
      return;
    }
    if (!prewarm && response_status && structured && !structured->Terminated() &&
        !(responses && finish_reason == "length" &&
          structured->Failure() == GrammarFailure::kNone))
      response_status = Status::Fail(
          "constrained response did not reach a grammar-accepted stop token");
    // Both transports use the same parser. Never publish a successful terminal
    // (or a session snapshot) for malformed/truncated model tool output.
    if (!prewarm && response_status.ok()) {
      if (responses || completion || stream || text_stop.Enabled() ||
          sampling.logprobs || prediction_enabled) {
        std::vector<detail::Utf8Span> spans;
        const auto tail = utf8.Finish(visible_logprobs ? &spans : nullptr);
        if (visible_logprobs) token_logprobs.Map(spans);
        write_content(text_stop.Append(tail));
        if (response_status) write_content(text_stop.Finish());
        if (text_stop.Matched()) finish_reason = "stop";
      } else {
        std::vector<std::uint32_t> gen_u32(generated.begin(), generated.end());
        std::string text;
        {
          auto lock = diagnostic.Lock(tok_mu_, Wait::kTokenizer);
          response_status = tok_->Decode(gen_u32, true, &text);
        }
        if (!prewarm && response_status.ok()) {
          std::vector<ResponseDelta> ignored;
          response_status =
              response_parser.Append(detail::NormalizeUtf8(text), &ignored);
        }
      }
    }
    if (!prewarm && response_status.ok()) {
      std::vector<ResponseDelta> deltas;
      response_status =
          responses && finish_reason == "length"
              ? response_parser.FinishIncomplete(&deltas)
              : response_parser.Finish(finish_reason == "length", &deltas,
                                       text_stop.Matched());
      if (response_status.ok()) write_deltas(deltas);
    }
    if (text_stop.Enabled()) {
      std::fprintf(stderr,
                   "[text-stop] request=%s matched_index=%d tokens=%zu "
                   "mtp_steps=%d mtp_tokens=%d mtp_discarded=%d\n",
                   request_id.c_str(), text_stop.MatchedIndex(),
                   generated.size(), stop_mtp_steps, stop_mtp_tokens,
                   stop_mtp_discarded);
    }
    if (!response_status.ok()) {
      err_guard.ok = false;
      const char* code = !structured ? "response_parse_failed"
                         : finish_reason == "length"
                             ? "structured_output_incomplete"
                             : "structured_output_invalid";
      if (structured) {
        switch (structured->Failure()) {
          case GrammarFailure::kDependency:
            code = "constraint_dependency_unavailable";
            break;
          case GrammarFailure::kResource:
            code = "constraint_resource_exhausted";
            break;
          case GrammarFailure::kTimeout:
            code = "constraint_step_timeout";
            break;
          case GrammarFailure::kWorker:
            code = "constraint_worker_failed";
            break;
          default:
            break;
        }
      }
      const int status =
          structured && (structured->Failure() == GrammarFailure::kDependency ||
                         structured->Failure() == GrammarFailure::kResource ||
                         structured->Failure() == GrammarFailure::kTimeout)
              ? 503
              : 502;
      if (moderation_step) {
        cleanup();
        if (RejectUnhealthy(fd)) return;
        if (!review_failed(code, response_status.message())) return;
        continue;
      }
      diagnostic.Outcome("error", code);
      const detail::WriteOptions terminal{std::chrono::steady_clock::now() +
                                          std::chrono::milliseconds(250)};
      detail::SendResponseParseError(fd, stream, response_state,
                                     response_status.message(), terminal, code,
                                     status);
      cleanup();
      return;
    }
    if (finish_reason == "stop" && response_parser.HasToolCalls())
      finish_reason = response_parser.ToolFinishReason();
    detail::ServeRequestFaultPoint("handler.before_response",
                                   request_id.c_str());
    // Resolve device ownership before a success terminal or cache commit. The
    // slot/request leases remain held through output and the remaining cleanup.
    release_buffers();
    if (!buffers_ok) {
      err_guard.ok = false;
      diagnostic.Outcome("error", "device_memory_failure");
      SendError(fd, 503, "device buffer release failed; restart server", {},
                "gpu_unhealthy");
      return;
    }
    if (internal) {
      internal->content = response_parser.Content();
      internal->finish_reason = finish_reason;
      internal->input_tokens = T;
      internal->output_tokens = generated.size();
      observe_completion(generated.size());
      trace_outcome = trace::RequestOutcome::kSuccess;
      cleanup();
      if (control->Cancelled() || RejectUnhealthy(fd)) return;
      internal->completed = true;
      err_guard.ok = true;
      diagnostic.Outcome("success", "input_prepared");
      return;
    }
    // A choice is complete, but the external request retains admission,
    // cancellation and quota ownership until all choices finish.
    if (responses) {
      if (moderation_step) {
        io::Json report;
        s = finish_reason == "length"
                ? Status::Fail("Classification exhausted its output budget")
                : ParseModerationResult(response_parser.Content(),
                                        moderation_plan, 0.5, &report);
        if (!s)
          report = ModerationError("moderation_scores_invalid", s.message());
        trace_outcome = s ? trace::RequestOutcome::kSuccess
                          : trace::RequestOutcome::kFailed;
        cleanup();
        if (!finish_review(input_moderation_step, std::move(report))) return;
        continue;
      }
      const ResponsesUsage step_usage{
          T, restored_prefix, static_cast<int64_t>(generated.size()),
          internal_step ? static_cast<int64_t>(generated.size())
                        : thinking_budget.ReasoningTokens(),
          std::max(0, cache_written_tokens - restored_prefix)};
      responses_usage.input_tokens += step_usage.input_tokens;
      responses_usage.cached_tokens += step_usage.cached_tokens;
      responses_usage.cache_write_tokens += step_usage.cache_write_tokens;
      responses_usage.output_tokens += step_usage.output_tokens;
      responses_usage.reasoning_tokens += step_usage.reasoning_tokens;
      if (!internal_step) {
        responses->CacheDiagnostics(prompt_cache_diagnostics_.Compare(
            client, cache_options.comparison_response_id,
            responses->Output().Snapshot(), ids, restored_prefix,
            cache_available, compacted_primary));
        cache_observation = PromptCacheDiagnostics::Observe(
            responses->Output().Snapshot(), ids,
            std::max(restored_prefix, cache_written_tokens), compacted_primary);
      }
      if (compaction_step) {
        if (finish_reason == "length") {
          err_guard.ok = false;
          SendError(fd, 502, "Compaction memory exhausted its output budget",
                    {}, "compaction_incomplete", "context_management");
          return;
        }
        std::string compact_wire;
        const auto compacted = responses->CompleteCompaction(
            response_parser.Content(), responses_usage.output_tokens,
            planning_config, measure_context, &compact_wire);
        if (!compacted.ok()) {
          err_guard.ok = false;
          SendError(fd, compacted.http_status, compacted.ClientMessage(), {},
                    compacted.ClientCode(), compacted.value.GetString("param"));
          return;
        }
        if (stream && !write_stream(compact_wire)) return;
        if (!standalone_compact) {
          continuation_generation = responses->Generation();
          if (pro_selected) {
            s = PlanResponsesDeliberation(
                continuation_generation, request_output_limit,
                responses_usage.output_tokens, planning_config, &pro_plan,
                &planning_error);
            if (!s) {
              err_guard.ok = false;
              SendError(fd, planning_error.http_status, s.message(), {},
                        planning_error.code, "reasoning.mode");
              return;
            }
          }
          response_step =
              pro_selected ? ResponseStep::kPro : ResponseStep::kPrimary;
          trace_outcome = trace::RequestOutcome::kSuccess;
          cleanup();
          continue;
        }
      }
      if (pro_step) {
        if (responses_usage.output_tokens == request_output_limit) {
          // Actual deliberation consumed the caller's whole remaining budget.
          // Publish an incomplete reasoning-only response, never refill it.
          finish_reason = "length";
        } else {
          ResponsesGenerationPlan answer;
          s = PlanResponsesDeliberatedAnswer(
              responses->Generation(), pro_notes, request_output_limit,
              responses_usage.output_tokens, planning_config, &answer,
              &planning_error);
          if (!s) {
            err_guard.ok = false;
            SendError(fd, planning_error.http_status, s.message(), {},
                      planning_error.code, "reasoning.mode");
            return;
          }
          continuation_generation = std::move(answer);
          response_step = ResponseStep::kPrimary;
          trace_outcome = trace::RequestOutcome::kSuccess;
          cleanup();
          continue;
        }
      }
      if (!internal_step && !prewarm && finish_reason != "length" &&
          !responses->Envelope().summary.empty()) {
        s = PlanResponsesSummary(
            responses->Envelope(), responses->Output().Snapshot(),
            request_output_limit, responses_usage.output_tokens,
            planning_config, &summary_plan, &planning_error);
        if (!s) {
          err_guard.ok = false;
          SendError(fd, planning_error.http_status, s.message(), {},
                    planning_error.code, "reasoning.summary");
          return;
        }
        if (summary_plan.needed) {
          responses_completed_calls = response_parser.CompletedToolCalls();
          response_step = ResponseStep::kSummary;
          trace_outcome = trace::RequestOutcome::kSuccess;
          cleanup();
          continue;
        }
      }
      if (summary_step && finish_reason != "length" &&
          !responses->Output().HasSummary()) {
        err_guard.ok = false;
        SendError(fd, 502, "Summary executor returned no summary", {},
                  "response_summary_empty", "reasoning.summary");
        return;
      }
      response_incomplete =
          finish_reason == "length" || summary_plan.budget_exhausted;
      if (!summary_step)
        responses_completed_calls = response_parser.CompletedToolCalls();
      if (moderate_output) {
        ModerationContent content;
        s = CollectModerationOutput(
            *responses->Output().Snapshot().GetArray("output"), &content,
            &planning_error);
        if (s)
          s = PlanModeration(moderation_options, content, planning_config,
                             kMaxModerationStepTokens, &output_moderation,
                             &planning_error);
        trace_outcome = trace::RequestOutcome::kSuccess;
        cleanup();
        if (!s) {
          if (!finish_review(false,
                             ModerationError("moderation_preparation_failed",
                                             s.message().substr(0, 1024))))
            return;
        } else {
          response_step = ResponseStep::kOutputReview;
        }
        continue;
      }
      const bool success = finish_response();
      trace_outcome = success ? trace::RequestOutcome::kSuccess
                              : trace::RequestOutcome::kFailed;
      cleanup();
      return;
    } else if (completion) {
      CompletionCandidate candidate;
      candidate.text = (completion_plan.echo ? completion_prompt->echo_text : "") +
                        response_parser.Content();
      candidate.finish = finish_reason;
      candidate.tokens = generated.size();
      candidate.sum_logprob = completion_score;
      candidate.score_tokens = completion_scored_tokens;
      if (visible_logprobs && !stream) {
        s = prompt_probabilities.Append(token_logprobs.CompletionRows());
        if (s) candidate.logprobs = prompt_probabilities.Json();
      }
      if (s) s = completion_choices.Add(choice_index, T, std::move(candidate));
    } else {
      s = choices.Add(
          choice_index, collect_chat ? response_parser.MessageJson() : "",
          finish_reason, sampling.logprobs ? token_logprobs.Json() : "null", T,
          prediction.Completion(generated.size()), restored_prefix,
          session.cache || !prefix_scope.empty(), reasoning_options.token_budget,
          thinking_budget.Consumed(), thinking_budget.ForcedTokens(),
          prediction_enabled, prediction.Accepted(), prediction.Rejected());
    }
    if (!s) {
      err_guard.ok = false;
      SendError(fd, 503, s.message(), {}, "response_storage_exhausted");
      return;
    }
    const bool final_choice = responses || choice_index + 1 == sampling.choices;
    if (final_choice) stop_heartbeat.Run();
    std::string stored_chat_response;
    if (!responses && !completion && final_choice &&
        (chat_storage.store || (!stream && chat_storage.has_metadata))) {
      s = choices.Json(id, model_field, created, &stored_chat_response);
      if (!s) {
        err_guard.ok = false;
        SendError(fd, 503, s.message(), {}, "response_storage_exhausted");
        return;
      }
      diagnostic.Stage("storage");
      RequestDiagnostics::Scope waiting(diagnostic, Wait::kStorage);
      io::Json input_snapshots;
      const BinaryAccess file_access{chat_state_access.scope,
                                     chat_state_access.deadline,
                                     chat_state_access.cancelled};
      auto stored = chat_storage.store
                        ? chat_files.Persist(id, file_access, &input_snapshots)
                        : ObjectReply{};
      if (stored.ok()) {
        stored = FinalizeStoredChat(objects_.get(), original_request,
                                    stored_chat_response, chat_state_access,
                                    input_snapshots);
        if (stored.ok() || stored.commit_unknown)
          chat_files.Published(file_access, stored.commit_unknown);
      }
      if (!stored.ok()) {
        err_guard.ok = false;
        SendError(fd, stored.http_status,
                  stored.ClientMessage() + "; completion_id=" + id, {},
                  stored.ClientCode(),
                  "store");
        return;
      }
      stored_chat_response = OutputJson(stored.value);
    }
    if (stream) {
      const auto finish_chunk = completion
          ? CompletionChunk(id, model_field, created, choice_index, "",
                              finish_reason,
                              visible_logprobs
                                  ? CompletionProbabilityRows().Json() : "null",
                              include_usage, completion_plan.include_obfuscation)
          : detail::SseDeltaChunk(
                id, model_field, "{}", finish_reason, choice_index, created,
                include_usage,
                sampling.logprobs ? R"({"content":[],"refusal":null})" : "null");
      std::string tail;
      if (final_choice) {
        if (include_usage) {
          tail = completion
              ? CompletionUsageChunk(id, model_field, created,
                                       completion_choices.UsageJson(),
                                       completion_plan.include_obfuscation)
              : "data: {\"id\":\"" + id +
                 "\",\"object\":\"chat.completion.chunk\",\"created\":" +
                 std::to_string(created) + ",\"model\":\"" +
                 JsonEscape(model_field) +
                 "\",\"choices\":[],\"usage\":" + choices.UsageJson() +
                 "}\r\n\r\n";
        }
        tail += "data: [DONE]\r\n\r\n";
        if (http) http->BeforeTerminal();
        *response_state = detail::ResponseState::kTerminal;
      }
      write_stream(finish_chunk);
      detail::ServeFaultPoint("handler.after_finish");
      if (final_choice) write_stream(tail);
    } else if (final_choice) {
      std::string resp;
      if (!stored_chat_response.empty()) {
        resp = std::move(stored_chat_response);
      } else {
        s = completion
              ? completion_choices.Json(id, model_field, created, &resp)
              : choices.Json(id, model_field, created, &resp);
      }
      if (!s) {
        err_guard.ok = false;
        SendError(fd, 503, s.message(), {}, "response_storage_exhausted");
        return;
      }
      if (!SendSimple(fd, 200, "OK", resp, "application/json",
                      {.control = control.get()})) {
        client_disconnected = true;
        control->Cancel(RequestControl::State::kDisconnect);
        count_abort();
      }
    }
    if (final_choice && !client_disconnected)
      *response_state = detail::ResponseState::kComplete;
    const bool response_success = !client_disconnected &&
                                  !control->Cancelled() &&
                                  (!final_choice || control->Finish());
    trace_outcome = response_success ? trace::RequestOutcome::kSuccess
                                     : trace::RequestOutcome::kCancelled;
    cleanup();
    if (!response_success) {
      count_abort();
      diagnostic.Outcome("cancelled", control->Reason());
      return;
    }
    if (final_choice) {
      metrics_.response_seconds.Observe(
          std::chrono::duration<double>(std::chrono::steady_clock::now() -
                                        t_arrive)
              .count());
      metrics_.e2e_seconds.Observe(
          std::chrono::duration<double>(std::chrono::steady_clock::now() -
                                        t_arrive)
              .count());
      request_session.success = true;
      for (auto& lease : shared_writes) lease->success = true;
      diagnostic.Outcome("success", "completed");
      metrics_.requests_success.fetch_add(1, std::memory_order_relaxed);
    }
  }  // Each choice releases its model/grammar buffers before the next one.
  if (responses)
    SendError(fd, 500, "Internal response step bound exceeded", {},
              "response_step_limit");
}

}  // namespace q4t::server
