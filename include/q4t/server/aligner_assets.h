// Optional pinned forced-alignment assets; reads stay in the isolated worker.
#pragma once

#include "q4t/server/asr_assets.h"

namespace q4t::server {
inline constexpr char kNativeAlignerProfile[] = "q4t-native-aligner-v1";
inline constexpr char kNativeAlignerRevision[] =
    "c7cbfc2048c462b0d63a45797104fc9db3ad62b7";
inline constexpr char kNativeAlignerWeightSha256[] =
    "47831d0e82f96b20e9034dba01a075ee06436654719f6a68289e49f1b65ce0e7";
inline constexpr size_t kNativeAlignerFileBytes = 1835544544;

// The byte owner/layout representation is shared; the loader strictly checks
// the independent 24-layer encoder, 5000-class head and timestamp tokenizer.
using AlignerHostAssets = AsrHostAssets;
Status LoadAlignerAssets(const std::string& directory, AlignerHostAssets* out,
                         GrammarControl control = {},
                         GrammarFailure* failure = nullptr,
                         bool include_weights = true,
                         size_t max_staging_bytes = 2ULL * 1024 * 1024 * 1024);
}  // namespace q4t::server
