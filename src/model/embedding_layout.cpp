#include "q4t/model/embedding_layout.h"

#include <algorithm>
#include <set>
#include <utility>

#include "q4t/model/embedding_config.h"

namespace q4t::model {

Status ParseEmbeddingWeightLayout(const io::Json& header, size_t data_bytes,
                                  EmbeddingWeightLayout* out) {
  if (!out || !header.IsObject() || data_bytes != EmbeddingWeightBytes())
    return Status::Fail("embedding weight payload size or header differs");
  const auto contracts = EmbeddingTensorContracts();
  std::set<std::string> names;
  for (const auto& [name, value] : header.object) {
    if (!names.insert(name).second)
      return Status::Fail("duplicate embedding tensor: " + name);
    if (name == "__metadata__" && !value.IsObject())
      return Status::Fail("invalid embedding weight metadata");
  }
  if (names.size() != contracts.size() + names.count("__metadata__"))
    return Status::Fail("embedding tensor set differs");
  EmbeddingWeightLayout layout;
  layout.bytes = data_bytes;
  std::vector<std::pair<size_t, size_t>> extents;
  for (const auto& expected : contracts) {
    const auto* tensor = header.Find(expected.name);
    const auto fail = [&] {
      return Status::Fail("embedding tensor contract differs: " +
                          expected.name);
    };
    if (!tensor || !tensor->IsObject() ||
        tensor->GetString("dtype") != "BF16") return fail();
    const auto* shape = tensor->GetArray("shape");
    const auto* offsets = tensor->GetArray("data_offsets");
    if (!shape || shape->array.size() != expected.shape.size() ||
        !offsets || offsets->array.size() != 2) return fail();
    for (size_t i = 0; i < expected.shape.size(); ++i)
      if (shape->array[i].AsInt(-1) != expected.shape[i]) return fail();
    const auto start = offsets->array[0].AsInt(-1);
    const auto end = offsets->array[1].AsInt(-1);
    if (start < 0 || end < start || start % 2 ||
        static_cast<uint64_t>(end) > data_bytes ||
        static_cast<uint64_t>(end - start) != expected.bytes) return fail();
    layout.tensors.push_back(
        {expected.name, static_cast<size_t>(start), expected.bytes});
    extents.emplace_back(start, end);
  }
  std::sort(extents.begin(), extents.end());
  size_t cursor = 0;
  for (const auto& [start, end] : extents) {
    if (start != cursor)
      return Status::Fail("embedding tensor gap or overlap");
    cursor = end;
  }
  if (cursor != data_bytes)
    return Status::Fail("embedding trailing weight payload");
  *out = std::move(layout);
  return {};
}

Status PlanEmbeddingWorkspace(int tokens, EmbeddingWorkspaceLayout* out) {
  using C = EmbeddingConfig;
  if (!out || tokens <= 0 || tokens > C::kInputTokens)
    return Status::Fail("embedding input must contain 1..8192 token IDs");
  EmbeddingWorkspaceLayout w;
  w.tokens = tokens;
  w.padded_tokens = (tokens + 15) / 16 * 16;
  const size_t t = tokens, p = w.padded_tokens;
  const auto carve = [&](size_t bytes) {
    const size_t start = (w.bytes + 255) / 256 * 256;
    w.bytes = start + bytes;
    return start;
  };
  const auto bf16 = [&](size_t elements) { return carve(elements * 2); };
  w.ids = carve(t * sizeof(int32_t));
  w.hidden = bf16(t * C::kHidden);
  w.normalized = bf16(t * C::kHidden);
  w.q = bf16(t * C::kQueryHeads * C::kHeadDim);
  w.k = bf16(t * C::kKvHeads * C::kHeadDim);
  w.v = bf16(t * C::kKvHeads * C::kHeadDim);
  w.head_q = bf16(p * C::kQueryHeads * C::kHeadDim);
  w.head_k = bf16(p * C::kKvHeads * C::kHeadDim);
  w.transposed_v = bf16(p * C::kKvHeads * C::kHeadDim);
  w.rope = bf16(t * C::kHeadDim);  // 64 cosine + 64 sine values per token.
  w.scores = bf16(t * p);
  w.head_output = bf16(t * C::kHeadDim);
  w.attention = bf16(t * C::kQueryHeads * C::kHeadDim);
  w.projected = bf16(t * C::kHidden);
  w.gate = bf16(t * C::kIntermediate);
  w.up = bf16(t * C::kIntermediate);
  w.gemm = carve(EmbeddingWorkspaceLayout::kGemmBytes);
  *out = w;
  return {};
}

}  // namespace q4t::model
