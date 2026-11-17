#include "chat_server_internal.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/audio/separator_resample.h"
#include "q4t/runtime/system_memory.h"
#include "q4t/server/request_diagnostics.h"
#include "q4t/server/speaker_reference.h"

namespace q4t::server {
ObjectReply ChatServer::PrepareSpeakerTracks(
    int fd, const AsrRequest& request, std::span<SpeakerTrack> tracks,
    const std::shared_ptr<RequestControl>& control,
    RequestDiagnostics& diagnostic) {
  using object_detail::Error;
  const auto cancelled = [&] {
    if (stop_requested_.load())
      control->Cancel(RequestControl::State::kShutdown);
    return detail::RequestCancelled(control.get(), fd);
  };
  const auto interrupted = [&] {
    return Error(409, control->Reason(), "Speaker preparation cancelled");
  };
  if (tracks.empty() || tracks.size() > 2 || request.speaker_names.size() > 4 ||
      request.speaker_names.size() != request.speaker_references.size())
    return Error(400, "speaker_reference_invalid",
                 "Speaker reference shape invalid");
  SpeakerResourcePlan resources;
  auto status = PlanSpeakerServiceResources(speaker_limits_, &resources);
  if (!status || !runtime::ReadSystemMemory().CanAllocate(
                     resources.request_host_bytes + (512ULL << 20)))
    return Error(503, "speaker_resource_exhausted",
                 "Speaker host memory unavailable");
  struct TrackWindow {
    size_t track;
    audio::SpeakerWindow window;
  };
  std::vector<TrackWindow> owned_windows;
  const size_t max_windows = speaker_limits_.max_windows;
  for (size_t i = 0; i < tracks.size(); ++i) {
    if (tracks[i].chunks.empty()) continue;
    if (owned_windows.size() >= max_windows)
      return Error(400, "speaker_window_limit", "Shared speaker window limit");
    std::vector<audio::SpeakerWindow> windows;
    status = audio::PlanSpeakerWindows(tracks[i].chunks, tracks[i].pcm.size(),
                                       {size_t(speaker_limits_.window_ms) * 16,
                                        size_t(speaker_limits_.step_ms) * 16,
                                        max_windows - owned_windows.size()},
                                       &windows, cancelled);
    if (!status) {
      if (cancelled()) return interrupted();
      return Error(400, "speaker_window_limit", status.message());
    }
    for (const auto& window : windows) owned_windows.push_back({i, window});
  }
  std::stable_sort(owned_windows.begin(), owned_windows.end(),
                   [](const auto& a, const auto& b) {
                     return a.window.core_begin_sample <
                            b.window.core_begin_sample;
                   });
  std::vector<audio::SpeakerWindow> windows;
  for (const auto& owned : owned_windows) windows.push_back(owned.window);
  const auto* http = detail::HttpResponseContext::Current(fd);
  if (!http || !http->Client().Allowed())
    return Error(503, "object_scope_unavailable", "Speaker owner unavailable");
  const auto& client = http->Client();
  const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                     : "local:anonymous";
  const BinaryAccess access{scope, control->deadline(), cancelled};
  std::vector<FileAudioPcm> references(request.speaker_references.size());
  std::array<std::vector<float>, 4> conditioned_references;
  for (size_t i = 0; i < references.size(); ++i) {
    RequestDiagnostics::Scope waiting(diagnostic,
                                      RequestDiagnostics::Wait::kMedia);
    diagnostic.Stage("speaker_reference");
    auto reply = DecodeSpeakerReference(request.speaker_references[i], access,
                                        &references[i]);
    if (!reply.ok()) return reply;
    if (tracks.size() > 1) {
      // Match the separated tracks' 8k analysis/reconstruction bandwidth.
      // Keep the validated original PCM/metadata owner intact. Never pass a
      // clean reference through the two-source network or relax cosine gates.
      std::vector<float> downsampled;
      status = audio::DownsampleSeparation(references[i].samples, 160000,
                                           &downsampled, cancelled);
      if (status)
        status = audio::RestoreSeparation(
            downsampled, references[i].samples.size(), 160000,
            &conditioned_references[i], cancelled);
      if (!status) {
        if (cancelled()) return interrupted();
        return Error(400, "speaker_reference_invalid", status.message());
      }
    }
  }
  std::vector<std::span<const float>> clips;
  for (size_t i = 0; i < references.size(); ++i)
    clips.push_back(tracks.size() > 1 ? conditioned_references[i]
                                      : references[i].samples);
  for (const auto& owned : owned_windows)
    clips.push_back(tracks[owned.track].pcm.subspan(
        owned.window.begin_sample,
        owned.window.end_sample - owned.window.begin_sample));
  std::vector<audio::SpeakerEmbedding> embeddings;
  auto reply = RunNativeSpeaker(fd, clips, control, diagnostic, &embeddings);
  if (!reply.ok()) return reply;
  std::vector<audio::SpeakerReference> known;
  for (size_t i = 0; i < references.size(); ++i)
    known.push_back({request.speaker_names[i], embeddings[i]});
  diagnostic.Stage("speaker_clustering");
  std::vector<audio::SpeakerTurn> labels;
  const audio::SpeakerClusteringOptions options{
      .max_windows = size_t(speaker_limits_.max_windows),
      .max_speakers = size_t(speaker_limits_.max_speakers)};
  // Single-track callers retain the existing coalescing/output contract.
  status = tracks.size() == 1
               ? audio::ClusterSpeakerWindows(
                     windows, std::span(embeddings).subspan(known.size()),
                     known, options, &labels, cancelled)
               : audio::LabelOverlappingSpeakerWindows(
                     windows, std::span(embeddings).subspan(known.size()),
                     known, options, &labels, cancelled);
  if (!status) {
    if (cancelled()) return interrupted();
    return Error(400, "speaker_clustering_invalid", status.message());
  }
  if (tracks.size() == 1) {
    tracks[0].turns = std::move(labels);
  } else {
    if (labels.size() != owned_windows.size())
      return Error(500, "speaker_clustering_invalid", "Speaker label count");
    std::array<std::vector<audio::SpeakerTurn>, 2> output;
    for (size_t i = 0; i < labels.size(); ++i)
      output[owned_windows[i].track].push_back(std::move(labels[i]));
    for (size_t i = 0; i < tracks.size(); ++i)
      tracks[i].turns = std::move(output[i]);
  }
  return {};
}
}  // namespace q4t::server
