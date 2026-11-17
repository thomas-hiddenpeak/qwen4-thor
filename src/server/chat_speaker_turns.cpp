#include "chat_server_internal.h"

#include "object_protocol.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/request_diagnostics.h"
#include "q4t/server/speaker_reference.h"

namespace q4t::server {
ObjectReply ChatServer::PrepareSpeakerTurns(
    int fd, const AsrRequest& request, std::span<const float> pcm,
    std::span<const audio::VadChunk> chunks,
    const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic, std::vector<audio::SpeakerTurn>* turns) {
  using object_detail::Error;
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(control.get(), fd);
  };
  const auto interrupted = [&] {
    return Error(409, control->Reason(), "Speaker preparation cancelled");
  };
  if (!turns || request.speaker_names.size() > 4 ||
      request.speaker_names.size() != request.speaker_references.size())
    return Error(400, "speaker_reference_invalid",
                 "Speaker reference shape invalid");
  SpeakerResourcePlan resources;
  auto status = PlanSpeakerServiceResources(speaker_limits_, &resources);
  if (!status || !runtime::ReadSystemMemory().CanAllocate(
                     resources.request_host_bytes + (512ULL << 20)))
    return Error(503, "speaker_resource_exhausted",
                 "Speaker host memory unavailable");
  std::vector<audio::SpeakerWindow> windows;
  status = audio::PlanSpeakerWindows(chunks, pcm.size(),
                                     {size_t(speaker_limits_.window_ms) * 16,
                                      size_t(speaker_limits_.step_ms) * 16,
                                      size_t(speaker_limits_.max_windows)},
                                     &windows, cancelled);
  if (!status) {
    if (cancelled()) return interrupted();
    return Error(400, "speaker_window_limit", status.message());
  }
  const auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed())
    return Error(503, "object_scope_unavailable", "Speaker owner unavailable");
  const auto& client = http->Client();
  const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                     : "local:anonymous";
  const BinaryAccess access{scope, control->deadline(), cancelled};
  std::vector<FileAudioPcm> references(request.speaker_references.size());
  for (size_t i = 0; i < references.size(); ++i) {
    RequestDiagnostics::Scope waiting(diagnostic,
                                      RequestDiagnostics::Wait::kMedia);
    diagnostic.Stage("speaker_reference");
    auto reply = DecodeSpeakerReference(request.speaker_references[i], access,
                                        &references[i]);
    if (!reply.ok()) return reply;
  }
  std::vector<std::span<const float>> clips;
  for (const auto& reference : references) clips.push_back(reference.samples);
  for (const auto& window : windows)
    clips.push_back(pcm.subspan(window.begin_sample,
                                window.end_sample - window.begin_sample));
  std::vector<audio::SpeakerEmbedding> embeddings;
  auto reply = RunNativeSpeaker(fd, clips, control, diagnostic, &embeddings);
  if (!reply.ok()) return reply;
  std::vector<audio::SpeakerReference> known;
  for (size_t i = 0; i < references.size(); ++i)
    known.push_back({request.speaker_names[i], embeddings[i]});
  diagnostic.Stage("speaker_clustering");
  status = audio::ClusterSpeakerWindows(
      windows, std::span(embeddings).subspan(known.size()), known,
      {.max_windows = size_t(speaker_limits_.max_windows),
       .max_speakers = size_t(speaker_limits_.max_speakers)},
      turns, cancelled);
  if (!status) {
    if (cancelled()) return interrupted();
    return Error(400, "speaker_clustering_invalid", status.message());
  }
  return {};
}
}  // namespace q4t::server
