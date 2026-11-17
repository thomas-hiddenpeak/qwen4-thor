// Shared, fixed-catalog optional-model I/O. Never initialize CUDA here.
#pragma once

#include <functional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/server/grammar_process.h"

namespace q4t::server {
inline constexpr int kModelAssetTimeoutMs = 60000;
int MaybeRunModelAssetWorker(int argc, char** argv);

namespace detail {
enum class ModelAssetProfile : uint64_t {
  kEmbedding = 1,
  kAsr = 2,
  kAligner = 3,
  kVad = 4
};
struct ModelAssetSpec {
  const char* name;
  size_t bytes;
  const char* sha256;
};

// Metadata in trained order followed by model.safetensors. Unknown profiles
// have no catalog. No caller-supplied names, sizes, digests or remote URLs.
std::span<const ModelAssetSpec> ModelAssetCatalog(ModelAssetProfile profile);
bool ValidModelAssetDirectory(std::string_view directory);

using ModelMetadataValidator =
    std::function<Status(std::span<const std::string>)>;

// Single shared optional-model I/O slot, independent of ordinary generation.
// Metadata is verified before invoking validate and allocating weight bytes.
// Failure preserves *weights. Caller publishes parsed metadata only on success.
// max_staging_bytes covers raw metadata + weights, not all live RAM; server
// admission must additionally account for parsed objects/device/file pages.
Status ReadVerifiedModelAssets(
    ModelAssetProfile profile, const std::string& directory,
    const ModelMetadataValidator& validate, std::vector<uint8_t>* weights,
    GrammarControl control, GrammarFailure* failure, bool include_weights,
    size_t max_staging_bytes = 2ULL * 1024 * 1024 * 1024);

// Check cancellation/deadline before publication or another bounded CPU stage.
Status CheckModelAssetControl(const GrammarControl& control,
                              GrammarFailure* failure);
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void SetModelAssetWorkerForTesting(std::string entry);
#endif
}  // namespace detail
}  // namespace q4t::server
