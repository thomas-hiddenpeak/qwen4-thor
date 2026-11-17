#include "q4t/server/vad_assets.h"

#include <algorithm>
#include <new>

namespace q4t::server {
Status LoadVadAssets(const std::string& directory, audio::SileroVad* out,
                     GrammarControl control, GrammarFailure* failure,
                     size_t max_staging_bytes) {
  if (failure) *failure = GrammarFailure::kNone;
  const auto fail = [&](GrammarFailure kind, const char* message) {
    if (failure) *failure = kind;
    return Status::Fail(message);
  };
  if (!out) return fail(GrammarFailure::kDependency, "missing VAD owner");
  control.compile_deadline =
      std::min(control.compile_deadline,
               std::chrono::steady_clock::now() + std::chrono::seconds(5));
  try {
    std::vector<uint8_t> file;
    auto status = detail::ReadVerifiedModelAssets(
        detail::ModelAssetProfile::kVad, directory,
        [](std::span<const std::string> metadata) {
          return metadata.empty() ? Status{}
                                  : Status::Fail("unexpected VAD metadata");
        },
        &file, control, failure, true, max_staging_bytes);
    if (!status) return status;
    audio::SileroVad next;
    status = next.Load(file, [&] {
      return !detail::CheckModelAssetControl(control, failure);
    });
    if (auto s = detail::CheckModelAssetControl(control, failure); !s) return s;
    if (!status && failure && *failure != GrammarFailure::kNone) return status;
    if (!status)
      return fail(GrammarFailure::kDependency, "VAD tensor layout invalid");
    *out = std::move(next);
    return {};
  } catch (const std::bad_alloc&) {
    return fail(GrammarFailure::kResource, "VAD host staging exhausted");
  }
}
}  // namespace q4t::server
