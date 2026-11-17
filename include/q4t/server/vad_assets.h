#pragma once

#include "q4t/audio/vad.h"
#include "q4t/server/model_assets.h"

namespace q4t::server {
// Fixed CPU-only asset, verified by the existing isolated model I/O worker.
// No CUDA or third-party inference runtime. Failure retains the prior owner.
Status LoadVadAssets(const std::string& directory, audio::SileroVad* out,
                     GrammarControl control = {},
                     GrammarFailure* failure = nullptr,
                     size_t max_staging_bytes = audio::kVadFileBytes);
}  // namespace q4t::server
