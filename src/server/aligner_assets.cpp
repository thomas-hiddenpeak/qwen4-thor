#include "q4t/server/aligner_assets.h"

#include <algorithm>
#include <array>
#include <new>

#include "q4t/audio/aligner_layout.h"

namespace q4t::server {
Status LoadAlignerAssets(const std::string& directory, AlignerHostAssets* out,
                         GrammarControl control, GrammarFailure* failure,
                         bool include_weights, size_t max_staging_bytes) {
  if (failure) *failure = GrammarFailure::kNone;
  const auto fail = [&](GrammarFailure kind, const char* message) {
    if (failure) *failure = kind;
    return Status::Fail(message);
  };
  if (!out)
    return fail(GrammarFailure::kDependency, "missing alignment destination");
  control.compile_deadline =
      std::min(control.compile_deadline,
               std::chrono::steady_clock::now() +
                   std::chrono::milliseconds(kModelAssetTimeoutMs));
  try {
    AlignerHostAssets next;
    io::JsonParseLimits limits{.max_depth = 16,
                               .max_values = 32768,
                               .max_string_bytes = 1024 * 1024,
                               .reject_duplicate_keys = true};
    const auto metadata = [&](std::span<const std::string> files) {
      using Validator = Status (*)(const io::Json&);
      constexpr std::array<Validator, 3> validators{
          audio::ValidateAlignerConfig,
          audio::ValidateAlignerPreprocessorConfig,
          audio::ValidateAlignerGenerationConfig};
      for (size_t i = 0; i < validators.size(); ++i) {
        io::Json parsed;
        auto status = io::ParseJson(files[i], &parsed, false, limits);
        if (status) status = validators[i](parsed);
        if (!status) return status;
        if (auto s = detail::CheckModelAssetControl(control, failure); !s)
          return s;
      }
      text::TokenizerLimits tokenizer_limits;
      tokenizer_limits.max_tokenizer_bytes = 5 * 1024 * 1024;
      tokenizer_limits.max_vocab_size = 151643;
      tokenizer_limits.max_merges = 151387;
      tokenizer_limits.max_added_tokens = 63;
      return text::Tokenizer::LoadQwen3Aligner(
          files[3], files[4], files[5], tokenizer_limits, &next.tokenizer);
    };
    auto status = detail::ReadVerifiedModelAssets(
        detail::ModelAssetProfile::kAligner, directory, metadata,
        &next.file_bytes, control, failure, include_weights, max_staging_bytes);
    if (!status) return status;
    if (include_weights) {
      uint64_t header_bytes = 0;
      for (int i = 0; i < 8; ++i)
        header_bytes |= static_cast<uint64_t>(next.file_bytes[i]) << (8 * i);
      if (!header_bytes || header_bytes > 1024 * 1024 ||
          header_bytes + 8 >= next.file_bytes.size())
        return fail(GrammarFailure::kDependency,
                    "alignment weight header invalid");
      next.data_offset = header_bytes + 8;
      const std::string header(
          reinterpret_cast<const char*>(next.file_bytes.data() + 8),
          header_bytes);
      io::Json parsed;
      status = io::ParseJson(header, &parsed, false, limits);
      if (status)
        status = audio::ParseAlignerWeightLayout(
            parsed, next.WeightBytes().size(), &next.layout);
      if (!status)
        return fail(GrammarFailure::kDependency,
                    "alignment tensor layout differs");
    }
    if (auto s = detail::CheckModelAssetControl(control, failure); !s) return s;
    *out = std::move(next);
    return {};
  } catch (const std::bad_alloc&) {
    return fail(GrammarFailure::kResource, "alignment host staging exhausted");
  }
}
}  // namespace q4t::server
