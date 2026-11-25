#include "q4t/server/chatkit.h"

#include <algorithm>

#include "file_audio_protocol.h"

#include "q4t/audio/asr_config.h"
#include "q4t/server/asr_request.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/realtime_transcription_protocol.h"

namespace q4t::server {
namespace {
using audio_detail::Container;

bool MatchesAudioType(std::string_view mime, Container actual) {
  const auto semi = mime.find(';');
  std::string type(mime.substr(0, semi));
  while (!type.empty() && type.back() == ' ') type.pop_back();
  for (auto& c : type)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  if (actual == Container::kUnknown) return false;
  if (type == "application/octet-stream") return true;
  switch (actual) {
    case Container::kWave:
      return type == "audio/wav" || type == "audio/wave" ||
             type == "audio/x-wav" || type == "audio/vnd.wave";
    case Container::kFlac:
      return type == "audio/flac" || type == "audio/x-flac";
    case Container::kOgg:
      return type == "audio/ogg" || type == "application/ogg" ||
             type == "audio/opus";
    case Container::kMpeg:
      return type == "audio/mpeg" || type == "audio/mp3";
    case Container::kAdts:
      return type == "audio/aac" || type == "audio/aacp";
    case Container::kMp4:
      return type == "audio/mp4" || type == "audio/x-m4a" ||
             type == "video/mp4";
    case Container::kWebm:
      return type == "audio/webm" || type == "video/webm" ||
             type == "audio/x-matroska" || type == "video/x-matroska";
    case Container::kUnknown: return false;
  }
  return false;
}
}  // namespace

Status ParseChatKitTranscription(const io::Json& params, AsrRequest* request,
                                 ChatPlanError* error) {
  *error = {"invalid_request", "params", 400};
  auto fields = InputFields(params, {"audio_base64", "mime_type"},
                            &error->param, "params");
  if (!fields) return fields;
  const auto* mime = params.Find("mime_type");
  if (!mime || !mime->IsString() || mime->str.empty() ||
      mime->str.size() > 255 ||
      std::ranges::any_of(mime->str, [](unsigned char c) {
        return c < 0x20 || c > 0x7e;
      })) {
    error->param = "params.mime_type";
    return Status::Fail("Expected an audio media type");
  }
  const auto* encoded = params.Find("audio_base64");
  error->param = "params.audio_base64";
  if (!encoded || !encoded->IsString() || encoded->str.empty())
    return Status::Fail("Expected base64-encoded audio");
  AsrRequest next;
  auto decoded = DecodeAudioBase64(encoded->str, kAsrFileBytes, &next.file);
  if (!decoded.ok()) {
    if (decoded.code == "audio_too_large") {
      error->code = decoded.code;
      error->http_status = 413;
    }
    return Status::Fail(decoded.ClientMessage());
  }
  if (!MatchesAudioType(mime->str, audio_detail::Detect(next.file))) {
    *error = {"unsupported_media_type", "params.mime_type", 415};
    return Status::Fail("Recording format does not match its media type");
  }
  next.model = audio::AsrConfig::kModelId;
  next.format = AsrFormat::kText;
  *request = std::move(next);
  *error = {};
  return {};
}
}  // namespace q4t::server
