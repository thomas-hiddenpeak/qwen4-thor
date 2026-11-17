#include "q4t/audio/asr_config.h"

#include <array>
#include <string_view>

#include "asr_config_internal.h"

namespace q4t::audio {
namespace {
using C = AsrConfig;
using detail::Fail;
using detail::Fields;
using detail::UniqueKeys;
using io::Json;

constexpr std::array<std::string_view, 30> kLanguages{
    "Chinese", "English", "Cantonese",  "Arabic",     "German",
    "French",  "Spanish", "Portuguese", "Indonesian", "Italian",
    "Korean",  "Russian", "Thai",       "Vietnamese", "Japanese",
    "Turkish", "Hindi",   "Malay",      "Dutch",      "Swedish",
    "Danish",  "Finnish", "Polish",     "Czech",      "Filipino",
    "Persian", "Greek",   "Romanian",   "Hungarian",  "Macedonian"};
}  // namespace

Status ValidateAsrConfig(const Json& config) {
  return detail::ValidateSpeechConfig<C>(config, kLanguages);
}

Status ValidateAsrPreprocessorConfig(const Json& config) {
  if (!UniqueKeys(config)) return Fail("preprocessor duplicate key or depth");
  if (auto s = Fields(config, "preprocessor.",
                      {{"feature_size", C::kMelBins},
                       {"hop_length", C::kHop},
                       {"n_fft", C::kFft},
                       {"chunk_length", 30},
                       {"n_samples", 480000},
                       {"nb_max_frames", 3000},
                       {"dither", 0},
                       {"padding_value", 0}},
                      {{"feature_extractor_type", "WhisperFeatureExtractor"},
                       {"processor_class", "Qwen3ASRProcessor"},
                       {"padding_side", "right"}},
                      {{"return_attention_mask", true}});
      !s)
    return s;
  // The official asset omits sampling_rate and relies on Whisper's 16 kHz
  // default. Explicit metadata must agree; no resampling by reinterpretation.
  if (config.Has("sampling_rate"))
    return Fields(config, "preprocessor.", {{"sampling_rate", C::kSampleRate}});
  return {};
}

Status ValidateAsrGenerationConfig(const Json& config) {
  if (!UniqueKeys(config)) return Fail("generation duplicate key or depth");
  if (auto s = Fields(config, "generation.",
                      {{"pad_token_id", C::kEos}, {"temperature", 1e-6}}, {},
                      {{"do_sample", false}, {"_from_model_config", true}});
      !s)
    return s;
  const auto* eos = config.GetArray("eos_token_id");
  if (!eos || eos->array.size() != 2 || eos->array[0].AsInt(-1) != C::kEos ||
      eos->array[1].AsInt(-1) != C::kImEnd || config.object.size() != 5)
    return Fail("generation.eos/extra fields");
  return {};
}

std::vector<AsrTensorContract> AsrTensorContracts() {
  return detail::SpeechTensorContracts<C>(C::kVocab);
}

size_t AsrWeightBytes() {
  size_t bytes = 0;
  for (const auto& tensor : AsrTensorContracts()) bytes += tensor.bytes;
  return bytes;
}

size_t AsrEncodedFrames(size_t mel_frames) {
  const size_t tail = mel_frames % C::kMelChunk;
  // Three ceil-divide-by-two convolutions on at most 99 tail frames.
  return mel_frames / C::kMelChunk * C::kEncodedChunk + (tail + 7) / 8;
}

Status ValidateAsrTensorHeader(const Json& header, uint64_t data_bytes) {
  return detail::ValidateSpeechTensorHeader(
      header, data_bytes, AsrTensorContracts(), AsrWeightBytes());
}

}  // namespace q4t::audio
