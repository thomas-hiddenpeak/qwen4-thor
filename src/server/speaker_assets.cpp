#include "q4t/server/speaker_assets.h"

#include <algorithm>
#include <new>

namespace q4t::server {
Status LoadSpeakerAssets(const std::string& directory, SpeakerHostAssets* out,
                         GrammarControl control, GrammarFailure* failure,
                         bool include_weights, size_t max_staging_bytes) {
  if (failure) *failure = GrammarFailure::kNone;
  const auto fail = [&](GrammarFailure kind, const char* message) {
    if (failure) *failure = kind;
    return Status::Fail(message);
  };
  if (!out) return fail(GrammarFailure::kDependency, "missing speaker owner");
  control.compile_deadline =
      std::min(control.compile_deadline,
               std::chrono::steady_clock::now() +
                   std::chrono::milliseconds(kModelAssetTimeoutMs));
  try {
    SpeakerHostAssets next;
    io::JsonParseLimits limits{.max_depth = 8,
                               .max_values = 8192,
                               .max_string_bytes = 65536,
                               .reject_duplicate_keys = true};
    const auto metadata = [&](std::span<const std::string> files) {
      if (files.size() != 1)
        return Status::Fail("speaker metadata inventory differs");
      io::Json parsed;
      auto status = io::ParseJson(files.front(), &parsed, false, limits);
      if (status) status = audio::ValidateSpeakerConfig(parsed);
      return status;
    };
    auto status = detail::ReadVerifiedModelAssets(
        detail::ModelAssetProfile::kSpeaker, directory, metadata,
        &next.file_bytes, control, failure, include_weights, max_staging_bytes);
    if (!status) return status;
    if (include_weights) {
      using C = audio::SpeakerConfig;
      if (next.file_bytes.size() != C::kFileBytes)
        return fail(GrammarFailure::kDependency, "speaker file size differs");
      uint64_t header_bytes = 0;
      for (int i = 0; i < 8; ++i)
        header_bytes |= static_cast<uint64_t>(next.file_bytes[i]) << (8 * i);
      if (header_bytes != C::kHeaderBytes)
        return fail(GrammarFailure::kDependency, "speaker header size differs");
      next.data_offset = header_bytes + 8;
      const std::string header(
          reinterpret_cast<const char*>(next.file_bytes.data() + 8),
          header_bytes);
      io::Json parsed;
      status = io::ParseJson(header, &parsed, false, limits);
      if (status)
        status = audio::ParseSpeakerTensorLayout(
            parsed, next.WeightBytes().size(), &next.layout,
            [&] { return !detail::CheckModelAssetControl(control, failure); });
      if (auto s = detail::CheckModelAssetControl(control, failure); !s)
        return s;
      if (!status) {
        if (failure && *failure != GrammarFailure::kNone) return status;
        return fail(GrammarFailure::kDependency,
                    "speaker tensor layout differs");
      }
    }
    if (auto s = detail::CheckModelAssetControl(control, failure); !s) return s;
    *out = std::move(next);
    return {};
  } catch (const std::bad_alloc&) {
    return fail(GrammarFailure::kResource, "speaker host staging exhausted");
  }
}
}  // namespace q4t::server
