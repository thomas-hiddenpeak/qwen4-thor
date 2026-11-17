#include "q4t/server/speaker_reference.h"

#include "object_protocol.h"

namespace q4t::server {
using namespace object_detail;
ObjectReply ReadSpeakerReference(std::string_view data,
                                 const BinaryAccess& access,
                                 FileInputContent* out) {
  if (!out || data.empty() || data.size() > (1ULL << 20) ||
      !data.starts_with("data:"))
    return Error(400, "speaker_reference_invalid",
                 "Speaker reference requires a bounded audio data URL");
  const auto part = Object({{"type", String("input_file")},
                            {"file_data", String(std::string(data))},
                            {"filename", String("reference.audio")}});
  FileInputSource source;
  auto reply = ParseFileInputSource(part, FileInputProfile::kResponses,
                                    "known_speaker_references", &source);
  if (!reply.ok()) return reply;
  if (source.kind != FileInputSource::Kind::kInline ||
      !source.mime_type.starts_with("audio/"))
    return Error(400, "speaker_reference_invalid",
                 "Speaker reference requires an audio media type");
  FileInputBudget budget{.max_bytes = 1ULL << 20, .max_files = 1};
  FileInputContent content;
  reply = ReadLocalFileInput(nullptr, access, source, &budget, &content);
  if (!reply.ok()) return reply;
  if (!IsAudioContainer(content.bytes))
    return Error(400, "speaker_reference_invalid",
                 "Speaker reference is not a supported audio container");
  *out = std::move(content);
  return {};
}
ObjectReply DecodeSpeakerReference(std::string_view data,
                                   const BinaryAccess& access,
                                   FileAudioPcm* out) {
  if (!out)
    return Error(500, "speaker_reference_owner_invalid",
                 "Speaker reference owner unavailable");
  FileInputContent content;
  auto reply = ReadSpeakerReference(data, access, &content);
  if (!reply.ok()) return reply;
  FileAudioLimits limits;
  limits.max_source_bytes = 1ULL << 20;
  limits.max_output_samples = 160000;
  limits.max_duration_ns = 10000000000ULL;
  FileAudioPcm pcm;
  reply = DecodeAudioContainer(content.bytes, limits, access, &pcm);
  if (!reply.ok()) return reply;
  if (pcm.samples.size() < 32000 || pcm.samples.size() > 160000)
    return Error(400, "speaker_reference_duration",
                 "Speaker reference must contain 2 to 10 seconds of audio");
  *out = std::move(pcm);
  return {};
}
}  // namespace q4t::server
