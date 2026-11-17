#include "q4t/audio/aligner_config.h"

#include <array>
#include <string_view>

#include "asr_config_internal.h"

namespace q4t::audio {
namespace {
using C = AlignerConfig;
using io::Json;
constexpr std::array<std::string_view, 11> kLanguages{
    "Chinese", "Cantonese",  "English", "German", "Spanish", "French",
    "Italian", "Portuguese", "Russian", "Korean", "Japanese"};
}  // namespace

Status ValidateAlignerConfig(const Json& config) {
  auto status =
      detail::ValidateSpeechConfig<C>(config, kLanguages, C::kClasses);
  if (!status) return status;
  return detail::Fields(
      config, "alignment.",
      {{"timestamp_token_id", C::kTimestampToken},
       {"timestamp_segment_time", C::kTimestampMilliseconds}});
}

Status ValidateAlignerPreprocessorConfig(const Json& config) {
  // Both fixed models use the exact same 16 kHz/128-bin Whisper features.
  return ValidateAsrPreprocessorConfig(config);
}

Status ValidateAlignerGenerationConfig(const Json& config) {
  if (!detail::UniqueKeys(config) || config.object.size() != 4)
    return detail::Fail("alignment generation keys/depth");
  auto status = detail::Fields(
      config, "alignment generation.", {{"pad_token_id", C::kEos}}, {},
      {{"do_sample", false}, {"_from_model_config", true}});
  if (!status) return status;
  const auto* eos = config.GetArray("eos_token_id");
  if (!eos || eos->array.size() != 2 || eos->array[0].AsInt(-1) != C::kEos ||
      eos->array[1].AsInt(-1) != C::kImEnd)
    return detail::Fail("alignment generation eos");
  return {};
}

std::vector<AsrTensorContract> AlignerTensorContracts() {
  return detail::SpeechTensorContracts<C>(C::kClasses);
}

size_t AlignerWeightBytes() {
  size_t bytes = 0;
  for (const auto& tensor : AlignerTensorContracts()) bytes += tensor.bytes;
  return bytes;
}

Status ValidateAlignerTensorHeader(const Json& header, uint64_t data_bytes) {
  return detail::ValidateSpeechTensorHeader(
      header, data_bytes, AlignerTensorContracts(), AlignerWeightBytes());
}

}  // namespace q4t::audio
