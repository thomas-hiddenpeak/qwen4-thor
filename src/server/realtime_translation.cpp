#include "q4t/server/realtime_translation.h"

#include "object_protocol.h"
#include "q4t/audio/asr_config.h"
#include "q4t/server/asr_request.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid(std::string param, std::string message,
                    bool model = false) {
  auto reply =
      Error(model ? 404 : 400, model ? "model_not_found" : "invalid_request",
            std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
}  // namespace

ObjectReply RealtimeTranslationSettings::Apply(const Json& config,
                                               bool create) {
  ChatPlanError failure;
  const auto shape = create ? RealtimeShape::kTranslationSessionCreate
                            : RealtimeShape::kTranslationSessionUpdate;
  const auto status =
      ValidateRealtimeWireShape(config, shape, &failure, "session");
  if (!status) return Invalid(failure.param, status.message());
  if (create && config.GetString("model") != kRealtimeTranslationModel)
    return Invalid("session.model", "Unknown translation model", true);
  const auto* audio = config.Find("audio");
  if (!audio) return {};
  auto next = *this;
  if (const auto* output = audio->Find("output")) {
    if (const auto* value = output->Find("language")) {
      if (AsrLanguageName(value->str).empty())
        return Invalid("session.audio.output.language",
                       "Unknown target language");
      next.language = value->str;
    }
  }
  if (const auto* input = audio->Find("input")) {
    if (const auto* asr = input->Find("transcription")) {
      next.transcription = asr->GetString("model");
      if (!asr->IsNull() && next.transcription != kRealtimeTranslationAsr &&
          next.transcription != audio::AsrConfig::kModelId)
        return Invalid("session.audio.input.transcription.model",
                       "Unknown source transcription model", true);
    }
    if (const auto* noise = input->Find("noise_reduction")) {
      using N = audio::NoiseReduction;
      if (noise->IsNull()) {
        next.noise = N::kOff;
        next.strength = 1;
      } else {
        next.noise = noise->GetString("type") == "near_field" ? N::kNearField
                                                              : N::kFarField;
        if (const auto* value = noise->Find("strength"))
          next.strength = value->number;
      }
    }
  }
  *this = std::move(next);
  return {};
}

Json RealtimeTranslationSettings::Resource(std::string_view id,
                                           int64_t expires_at) const {
  Json noise_config, asr;
  if (noise != audio::NoiseReduction::kOff) {
    noise_config =
        Object({{"type", String(noise == audio::NoiseReduction::kNearField
                                    ? "near_field"
                                    : "far_field")}});
    if (strength != 1) {
      Json gain;
      gain.type = Json::Type::kNumber;
      gain.number = strength;
      Set(&noise_config, "strength", std::move(gain));
    }
  }
  if (!transcription.empty()) asr = Object({{"model", String(transcription)}});
  auto input = Object({{"transcription", std::move(asr)},
                       {"noise_reduction", std::move(noise_config)}});
  return Object(
      {{"type", String("translation")},
       {"id", String(std::string(id))},
       {"model", String(std::string(kRealtimeTranslationModel))},
       {"expires_at", Number(expires_at)},
       {"audio",
        Object({{"input", std::move(input)},
                {"output", Object({{"language", String(language)}})}})}});
}
}  // namespace q4t::server
