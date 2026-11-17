#include "q4t/server/embedding_assets.h"

#include <algorithm>
#include <new>

#include "q4t/model/embedding_config.h"
#include "q4t/server/model_assets.h"

namespace q4t::server {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void detail::SetEmbeddingAssetWorkerForTesting(std::string entry) {
  SetModelAssetWorkerForTesting(std::move(entry));
}
#endif

Status LoadEmbeddingAssets(const std::string& directory,
                           EmbeddingHostAssets* out, GrammarControl control,
                           GrammarFailure* failure, bool include_weights) {
  if (failure) *failure = GrammarFailure::kNone;
  const auto fail = [&](GrammarFailure kind, const char* message) {
    if (failure) *failure = kind;
    return Status::Fail(message);
  };
  if (!out)
    return fail(GrammarFailure::kDependency, "missing embedding destination");
  control.compile_deadline =
      std::min(control.compile_deadline,
               std::chrono::steady_clock::now() +
                   std::chrono::milliseconds(kEmbeddingAssetTimeoutMs));
  try {
    EmbeddingHostAssets next;
    io::JsonParseLimits limits{.max_depth = 16,
                               .max_values = 32768,
                               .max_string_bytes = 1024 * 1024,
                               .reject_duplicate_keys = true};
    const auto metadata = [&](std::span<const std::string> files) {
      io::Json parsed;
      auto status = io::ParseJson(files[0], &parsed, false, limits);
      if (status) status = model::ValidateEmbeddingConfig(parsed);
      if (status)
        status =
            text::Tokenizer::LoadJson(files[1], {}, &next.tokenizer,
                                      text::TokenizerProfile::kQwen3Embedding);
      return status;
    };
    auto status = detail::ReadVerifiedModelAssets(
        detail::ModelAssetProfile::kEmbedding, directory, metadata,
        &next.file_bytes, control, failure, include_weights);
    if (!status) return status;
    if (include_weights) {
      uint64_t header_bytes = 0;
      for (int i = 0; i < 8; ++i)
        header_bytes |= static_cast<uint64_t>(next.file_bytes[i]) << (8 * i);
      if (!header_bytes || header_bytes > 1024 * 1024 ||
          header_bytes + 8 >= next.file_bytes.size())
        return fail(GrammarFailure::kDependency,
                    "embedding weight header invalid");
      next.data_offset = header_bytes + 8;
      const std::string header(
          reinterpret_cast<const char*>(next.file_bytes.data() + 8),
          header_bytes);
      io::Json parsed;
      status = io::ParseJson(header, &parsed, false, limits);
      if (status)
        status = model::ParseEmbeddingWeightLayout(
            parsed, next.WeightBytes().size(), &next.layout);
      if (!status)
        return fail(GrammarFailure::kDependency,
                    "embedding tensor layout differs");
    }
    if (auto s = detail::CheckModelAssetControl(control, failure); !s) return s;
    *out = std::move(next);
    return {};
  } catch (const std::bad_alloc&) {
    return fail(GrammarFailure::kResource, "embedding host staging exhausted");
  }
}

int MaybeRunEmbeddingAssetWorker(int argc, char** argv) {
  return MaybeRunModelAssetWorker(argc, argv);
}
}  // namespace q4t::server
