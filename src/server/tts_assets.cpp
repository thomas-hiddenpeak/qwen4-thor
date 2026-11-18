#include "q4t/server/tts_assets.h"

#include <algorithm>
#include <new>

namespace q4t::server {
namespace {
constexpr io::JsonParseLimits kJsonLimits{.max_depth = 16,
                                          .max_values = 32768,
                                          .max_string_bytes = 1024 * 1024,
                                          .reject_duplicate_keys = true};

Status Parse(std::string_view text, io::Json* out) {
  return io::ParseJson(std::string(text), out, false, kJsonLimits);
}

Status ParseWeights(TtsHostWeightFile* file, bool codec,
                    audio::TtsVariant variant,
                    audio::TtsWeightLayout* speaker,
                    audio::TtsWeightLayout* encoder = nullptr) {
  if (file->bytes.size() != (codec ? kNativeTtsCodecFileBytes
                             : variant == audio::TtsVariant::kBase17B
                                 ? kNativeTtsBaseFileBytes
                                 : kNativeTtsFileBytes))
    return Status::Fail("TTS weight file size differs");
  uint64_t header_bytes = 0;
  for (int i = 0; i < 8; ++i)
    header_bytes |= static_cast<uint64_t>(file->bytes[i]) << (8 * i);
  if (!header_bytes || header_bytes > 1024 * 1024 ||
      header_bytes + 8 >= file->bytes.size())
    return Status::Fail("TTS weight header size differs");
  file->data_offset = header_bytes + 8;
  io::Json header;
  auto status = Parse(
      {reinterpret_cast<const char*>(file->bytes.data() + 8), header_bytes},
      &header);
  if (!status) return status;
  if (!codec && variant == audio::TtsVariant::kBase17B) {
    status = audio::ParseTtsSpeakerWeightLayout(
        header, file->WeightBytes().size(), speaker);
    if (!status) return status;
  }
  if (codec && encoder) {
    status = audio::ParseTtsEncoderWeightLayout(
        header, file->WeightBytes().size(), encoder);
    if (!status) return status;
  }
  return codec ? audio::ParseTtsCodecWeightLayout(
                     header, file->WeightBytes().size(), &file->layout)
               : audio::ParseTtsWeightLayout(header, file->WeightBytes().size(),
                                             variant, &file->layout);
}

Status ValidateProcessor(const io::Json& p, bool codec) {
  if (!p.IsObject() ||
      p.GetString("padding_side") != (codec ? "right" : "left") ||
      p.GetNumber("padding_value", -1) != 0 ||
      !p.GetBool("return_attention_mask", false))
    return Status::Fail("TTS preprocessing contract differs");
  if (codec) {
    const auto* chunk = p.Find("chunk_length_s");
    const auto* overlap = p.Find("overlap");
    if (p.GetString("feature_extractor_type") != "EncodecFeatureExtractor" ||
        p.GetInt("feature_size") != 1 ||
        p.GetInt("sampling_rate") != audio::TtsConfig::kSampleRate || !chunk ||
        !chunk->IsNull() || !overlap || !overlap->IsNull())
      return Status::Fail("TTS codec preprocessing contract differs");
  } else if (p.GetString("processor_class") != "Qwen3TTSProcessor") {
    return Status::Fail("TTS processor class differs");
  }
  return {};
}
}  // namespace

Status LoadTtsAssets(const std::string& directory, TtsHostAssets* out,
                     GrammarControl control, GrammarFailure* failure,
                     bool include_weights, size_t max_staging_bytes,
                     TtsAssetSelection selection) {
  if (failure) *failure = GrammarFailure::kNone;
  const auto fail = [&](GrammarFailure kind, const char* message) {
    if (failure) *failure = kind;
    return Status::Fail(message);
  };
  if (!out || !selection.Valid() ||
      !detail::ValidModelAssetDirectory(directory) ||
      (selection.include_codec &&
       !detail::ValidModelAssetDirectory(directory + "/speech_tokenizer")))
    return fail(GrammarFailure::kDependency, "invalid TTS asset destination");
  // Reject the aggregate before opening any asset or allocating either model.
  const auto profile = selection.variant == audio::TtsVariant::kBase17B
                           ? detail::ModelAssetProfile::kTtsBase17B
                           : detail::ModelAssetProfile::kTtsCustomVoice17B;
  std::vector<detail::ModelAssetProfile> profiles{profile};
  if (selection.include_codec)
    profiles.push_back(detail::ModelAssetProfile::kTtsCodec);
  size_t total = 0;
  for (auto selected : profiles) {
    auto specs = detail::ModelAssetCatalog(selected);
    if (!include_weights) specs = specs.first(specs.size() - 1);
    for (const auto& spec : specs) {
      if (spec.bytes > max_staging_bytes - total)
        return fail(GrammarFailure::kResource, "TTS combined staging limit");
      total += spec.bytes;
    }
  }
  control.compile_deadline =
      std::min(control.compile_deadline,
               std::chrono::steady_clock::now() +
                   std::chrono::milliseconds(kModelAssetTimeoutMs));
  if (auto s = detail::CheckModelAssetControl(control, failure); !s) return s;
  try {
    TtsHostAssets next;
    next.variant = selection.variant;
    const auto talker_metadata = [&](std::span<const std::string> files) {
      if (files.size() != 6) return Status::Fail("TTS metadata count differs");
      for (size_t i = 0; i < 3; ++i) {
        io::Json parsed;
        auto status = Parse(files[i], &parsed);
        if (status) {
          if (i == 0)
            status = audio::ValidateTtsConfig(parsed, selection.variant);
          if (i == 1) status = ValidateProcessor(parsed, false);
          if (i == 2) status = audio::ValidateTtsGenerationConfig(parsed);
        }
        if (!status) return status;
        if (auto s = detail::CheckModelAssetControl(control, failure); !s)
          return s;
      }
      text::TokenizerLimits limits;
      limits.max_tokenizer_bytes = 5 * 1024 * 1024;
      limits.max_vocab_size = 151643;
      limits.max_merges = 151387;
      limits.max_added_tokens = 33;
      return text::Tokenizer::LoadQwen3Tts(files[3], files[4], files[5], limits,
                                           &next.tokenizer);
    };
    auto status = detail::ReadVerifiedModelAssets(
        profile, directory, talker_metadata, &next.talker.bytes, control,
        failure, include_weights, max_staging_bytes);
    if (!status) return status;
    if (include_weights && !ParseWeights(&next.talker, false, selection.variant,
                                         &next.speaker_layout))
      return fail(GrammarFailure::kDependency, "TTS talker layout differs");
    if (auto s = detail::CheckModelAssetControl(control, failure); !s) return s;
    if (!selection.include_codec) {
      *out = std::move(next);
      return {};
    }
    const auto codec_metadata = [&](std::span<const std::string> files) {
      if (files.size() != 3)
        return Status::Fail("TTS codec metadata count differs");
      io::Json config, processor, registration;
      auto s = Parse(files[0], &config);
      if (s) s = audio::ValidateTtsCodecConfig(config);
      if (s) s = Parse(files[1], &processor);
      if (s) s = ValidateProcessor(processor, true);
      if (s) s = Parse(files[2], &registration);
      // This is descriptive metadata only. allow_remote never enables code
      // execution; the native fixed graph has no Python or network entry.
      if (s && (!registration.IsObject() ||
                registration.GetString("framework") != "pytorch" ||
                registration.GetString("task") != "feature-extraction"))
        return Status::Fail("TTS codec registration differs");
      return s;
    };
    status = detail::ReadVerifiedModelAssets(
        detail::ModelAssetProfile::kTtsCodec, directory + "/speech_tokenizer",
        codec_metadata, &next.codec.bytes, control, failure, include_weights,
        max_staging_bytes - next.talker.bytes.size());
    if (!status) return status;
    if (include_weights &&
        !ParseWeights(&next.codec, true, selection.variant, nullptr,
                      selection.include_encoder ? &next.encoder_layout
                                                : nullptr))
      return fail(GrammarFailure::kDependency, "TTS codec layout differs");
    if (auto s = detail::CheckModelAssetControl(control, failure); !s) return s;
    *out = std::move(next);
    return {};
  } catch (const std::bad_alloc&) {
    return fail(GrammarFailure::kResource, "TTS host staging exhausted");
  }
}
}  // namespace q4t::server
