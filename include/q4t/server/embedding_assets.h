// Bounded optional model asset I/O in an exec-only native child.
#pragma once

#include <cstdint>
#include <span>
#include <vector>

#include "q4t/model/embedding_layout.h"
#include "q4t/server/grammar_process.h"
#include "q4t/text/tokenizer.h"

namespace q4t::server {
inline constexpr char kNativeEmbeddingModel[] = "qwen3-embedding-0.6b";
inline constexpr char kNativeEmbeddingRevision[] =
    "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3";
inline constexpr char kNativeEmbeddingWeightSha256[] =
    "0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd";
inline constexpr size_t kNativeEmbeddingFileBytes = 1191586416;
inline constexpr int kEmbeddingAssetTimeoutMs = 60000;

struct EmbeddingHostAssets {
  std::unique_ptr<text::Tokenizer> tokenizer;
  std::vector<uint8_t> file_bytes;
  model::EmbeddingWeightLayout layout;
  size_t data_offset = 0;
  std::span<const uint8_t> WeightBytes() const {
    return data_offset <= file_bytes.size()
        ? std::span(file_bytes).subspan(data_offset) : std::span<const uint8_t>{};
  }
};

// Never called under the model/GPU mutex. Parent performs no asset filesystem
// I/O. Exact asset fingerprints and tensor/config contracts must all pass
// before publishing host bytes. Failure leaves *out unchanged. Child owns no
// CUDA context; timeout/cancellation retains its slot until confirmed reap.
Status LoadEmbeddingAssets(const std::string& directory,
                            EmbeddingHostAssets* out,
                            GrammarControl control = {},
                            GrammarFailure* failure = nullptr,
                            bool include_weights = true);
int MaybeRunEmbeddingAssetWorker(int argc, char** argv);

namespace detail {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void SetEmbeddingAssetWorkerForTesting(std::string entry);
#endif
}  // namespace detail
}  // namespace q4t::server
