#include "chat_server_internal.h"

#include "object_protocol.h"
#include "q4t/server/request_diagnostics.h"

namespace q4t::server {
ObjectReply ChatServer::EncodeVectorDocument(std::string_view text,
                                             const VectorSearchControl& control,
                                             VectorEncodedText* output) {
  const auto cancelled = [&] {
    return stop_requested_.load() ||
           std::chrono::steady_clock::now() >= control.deadline ||
           (control.cancelled && control.cancelled());
  };
  auto loaded = EnsureNativeEmbedding(false, {{}, control.deadline, cancelled});
  if (!loaded.ok()) return loaded;
  auto lock = LockUnlessCancelled(embedding_tok_mu_, cancelled);
  if (!lock.owns_lock() || cancelled())
    return object_detail::Error(499, "vector_ingestion_cancelled",
                                "Ingestion tokenization cancelled");
  return EncodeVectorText(
      text,
      [&](auto source, auto* tokens) {
        return embedding_tok_->Encode(
            source, tokens,
            {.cancelled = cancelled, .add_special_tokens = false});
      },
      [&](uint32_t token, auto* piece) {
        return embedding_tok_->Decode({token}, false, piece);
      },
      control, output);
}
ObjectReply ChatServer::RunVectorEmbedding(
    const std::vector<int32_t>& tokens, const ClientIdentity& client,
    ClientLease& permit, const std::shared_ptr<RequestControl>& control,
    EmbeddingStepResult* result) {
  detail::HttpResponseContext http(detail::HttpResponseContext::Detached{});
  http.SetClient(client, &permit);
  if (!http.SetTraceId(control->id()))
    return object_detail::Error(500, "vector_execution_owner_invalid",
                                "Invalid ingestion execution identity");
  auto progress =
      progress_.Acquire(std::chrono::duration_cast<std::chrono::nanoseconds>(
                            control->deadline().time_since_epoch())
                            .count());
  control->BindProgress(progress.Handle());
  ScopeExit unbind([&] { control->BindProgress({}); });
  RequestDiagnostics diagnostic(stderr, control->id());
  diagnostic.BindProgress(progress.Handle());
  diagnostic.Bind(control->id());
  ProgressContext context(progress.Handle());
  return RunNativeEmbeddingInput(-1, tokens, control, diagnostic, result);
}
}  // namespace q4t::server
