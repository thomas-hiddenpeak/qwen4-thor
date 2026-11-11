#include "chat_server_internal.h"

#include <ctime>

#include "q4t/server/output_schema.h"
#include "q4t/server/responses_api.h"
#include "q4t/server/request_json.h"
#include "q4t/server/batch_budget.h"
#include "q4t/server/embeddings_plan.h"
#include "q4t/server/request_control.h"

namespace q4t::server {

bool ChatServer::TryHandleBackgroundResponse(int fd, const std::string& body) {
  io::Json request;
  const auto parsed =
      io::ParseJson(body, &request, true, kApplicationJsonLimits);
  // Foreground handling owns complete JSON/type diagnostics.
  if (!parsed || !request.GetBool("background")) return false;
  auto* http = detail::HttpResponseContext::Current(fd);
  if (!background_ || !objects_ || !http || !http->Client().Allowed()) {
    detail::SendError(fd, 503, "Background executor unavailable", {},
                      "background_unavailable");
    return true;
  }
  const auto client = http->Client();
  StateAccess access{client.Scoped() ? "client:" + std::string(client.Id())
                                     : "local:anonymous",
                     std::chrono::steady_clock::now() +
                         std::chrono::milliseconds(request_deadline_ms_),
                     [this, fd] {
                       return stop_requested_.load() ||
                              detail::ClientConnectionFailed(fd);
                     }};
  // The new owner takes the existing permit; there is no release/reacquire
  // gap. Subscriber disconnect and its HTTP deadline do not own generation.
  ServeBackgroundResponse(fd, *objects_, *background_, request,
                          PlanningConfig(fd), access);
  return true;
}

void ChatServer::RunBackgroundResponse(ResponsesJobs::Job& job) {
  detail::HttpResponseContext http(detail::HttpResponseContext::Detached{});
  http.SetClient(job.client, job.permit ? &*job.permit : nullptr,
                 job.run->RateDomain());
  http.SetTraceId(job.run->Id());
  // Observe actual failures. Do not use the live SSE error renderer here:
  // cancellation is finalized by the owner after all generation cleanup.
  http.SetErrorObserver(&ResponsesExecution::ObserveError, job.run.get());
  http.SetUsageObserver(&ResponsesExecution::ObserveUsage, job.run.get());
  http.Usage().Enable();
  if (!job.permit || !job.permit->Holds(job.client, ClientLane::kData)) {
    detail::SendError(-1, 500, "Background data ownership is invalid", {},
                      "background_admission_lost");
    return;
  }
  if (job.rate && !http.BindBatchRate(job.rate)) {
    detail::SendError(-1, 500, "Background rate ownership is invalid", {},
                      "background_rate_invalid");
    return;
  }
  HandleChatImpl(-1, "", http.State(), false, job.run.get(), job.control);
}

BatchTokenCodec ChatServer::BatchCodec(const BinaryAccess& access,
                                      bool native_embedding) {
  const auto cancelled = [this, access] {
    return stop_requested_.load() ||
           std::chrono::steady_clock::now() >= access.deadline ||
           (access.cancelled && access.cancelled());
  };
  BatchTokenCodec codec;
  codec.encode = [this, cancelled, native_embedding](std::string_view text,
                                   std::vector<uint32_t>* tokens) {
    auto& mutex = native_embedding ? embedding_tok_mu_ : tok_mu_;
    auto lock = LockUnlessCancelled(mutex, cancelled);
    if (!lock.owns_lock())
      return Status::Fail("Batch tokenizer wait cancelled");
    auto* tokenizer = native_embedding ? embedding_tok_.get() : tok_.get();
    if (!tokenizer) return Status::Fail("Batch tokenizer is unavailable");
    return tokenizer->Encode(text, tokens, {.cancelled = cancelled});
  };
  codec.decode = [this, cancelled, native_embedding](const std::vector<uint32_t>& tokens,
                                   std::string* text) {
    auto& mutex = native_embedding ? embedding_tok_mu_ : tok_mu_;
    auto lock = LockUnlessCancelled(mutex, cancelled);
    if (!lock.owns_lock())
      return Status::Fail("Batch tokenizer wait cancelled");
    auto* tokenizer = native_embedding ? embedding_tok_.get() : tok_.get();
    if (!tokenizer) return Status::Fail("Batch tokenizer is unavailable");
    return tokenizer->Decode(tokens, false, text);
  };
  codec.expand_media =
      [this](const io::Json& request, std::span<const VisionItem> images,
             const ChatPlanConfig& planning, std::vector<uint32_t>* tokens,
             ChatPromptBudget* prompt, ChatPlanError* error) {
        vision::MediaPlan media;
        const auto& model = model_.Get().cfg;
        return ExpandChatMedia(request, images, model.image_token_id,
                               model.video_token_id, proc_cfg_, video_proc_cfg_,
                               vision_device_budget_, planning, tokens, &media,
                               prompt, error);
      };
  return codec;
}

ObjectReply ChatServer::MeasureBatchRequest(std::string_view endpoint,
                                            const BatchInputRow& row,
                                            const io::Json& prepared,
                                            const ChatPlanConfig& config,
                                            const BinaryAccess& access,
                                            BatchInputBudget* budget,
                                            BatchRateBudget* rate) {
  if (!objects_) {
    ObjectReply reply;
    reply.http_status = 503;
    reply.code = "object_store_unavailable";
    reply.message = "Batch storage is unavailable";
    return reply;
  }
  auto bounded = access;
  bounded.cancelled = [this, access] {
    return stop_requested_.load() || (access.cancelled && access.cancelled());
  };
  const bool native = endpoint == "/v1/embeddings" &&
                      config.native_embedding_enabled &&
                      row.body.GetString("model") ==
                          model::EmbeddingConfig::kModelId;
  if (native) {
    const auto loaded = EnsureNativeEmbedding(false, bounded);
    if (!loaded.ok()) return loaded;
  }
  const auto codec = BatchCodec(bounded, native);
  const auto planning = endpoint == "/v1/embeddings"
      ? EmbeddingPlanningConfig(row.body.GetString("model"), config) : config;
  return MeasureBatchInput(*objects_, endpoint, row, prepared, planning, bounded,
                           codec, budget, rate);
}

ObjectReply ChatServer::MeasureBackgroundBatchRate(ResponsesExecution& response,
                                                   const ChatPlanConfig& config,
                                                   const BinaryAccess& access,
                                                   BatchRateBudget* budget) {
  auto bounded = access;
  bounded.cancelled = [this, access] {
    return stop_requested_.load() || (access.cancelled && access.cancelled());
  };
  return MeasureBatchResponseRate(response, config, bounded,
                                  BatchCodec(bounded), budget);
}

ObjectReply ChatServer::AdmitBackgroundBatch(ResponsesJobs::Job& job) {
  return AdmitBackgroundBatchRate(clients_, job, PlanningConfig(),
                                  [this](auto& response, const auto& config,
                                         const auto& access, auto* budget) {
                                    return MeasureBackgroundBatchRate(
                                        response, config, access, budget);
                                  });
}

BatchRowResult ChatServer::RunBatchRequest(
    const std::string& endpoint, const BatchInputRow& row,
    const io::Json& frozen_inputs, const ClientIdentity& client,
    ClientLease& permit, const std::shared_ptr<RequestControl>& control,
    std::shared_ptr<ClientRateLease> rate) {
  return ExecuteBatchEndpoint(
      control ? control->id() : "", client, permit, control,
      [&] {
        if (endpoint == "/v1/embeddings") {
          if (frozen_inputs.GetString("kind") != "embedding") {
            detail::SendError(-1, 503, "Invalid frozen embedding input", {},
                              "batch_prepared_input_invalid");
            return;
          }
          HandleEmbeddings(-1, OutputJson(row.body), control);
          return;
        }
        if (endpoint == "/v1/moderations") {
          if (frozen_inputs.GetString("kind") != "moderation") {
            detail::SendError(-1, 503, "Invalid frozen moderation input", {},
                              "batch_prepared_input_invalid");
            return;
          }
          HandleModerations(-1, OutputJson(row.body), control);
          return;
        }
        const auto api = endpoint == "/v1/responses" ? GenerationApi::kResponse
                        : endpoint == "/v1/completions"
                            ? GenerationApi::kCompletion
                            : GenerationApi::kChat;
        if (endpoint != "/v1/responses" && endpoint != "/v1/completions" &&
            endpoint != "/v1/chat/completions") {
          detail::SendError(-1, 503, "Native Batch executor unavailable", {},
                            "batch_endpoint_executor_unavailable");
          return;
        }
        if (stop_requested_.load()) {
          control->Cancel(RequestControl::State::kShutdown);
          detail::SendError(-1, 503, "Server is stopping", {},
                            "server_stopping");
          return;
        }
        HandleChat(
            -1, OutputJson(row.body), api, control, &frozen_inputs,
            row.background_response ? &*row.background_response : nullptr);
      },
      std::move(rate), endpoint);
}

}  // namespace q4t::server
