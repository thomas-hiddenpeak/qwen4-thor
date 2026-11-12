#include "q4t/server/vector_index.h"

#include <algorithm>
#include <array>
#include <bit>
#include <cmath>

#include "object_protocol.h"
#include "q4t/model/embedding_config.h"
#include "q4t/server/embedding_assets.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/input_snapshot_bindings.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
using C = model::EmbeddingConfig;
constexpr size_t kRawTokens = 4ULL << 20;
const InputSnapshotLimits kStorage{64ULL << 20, 2ULL << 20, 2};

ObjectReply Invalid() {
  return Error(503, "vector_index_invalid",
               "Retrieval index identity or content is inconsistent");
}
ObjectReply Capacity() {
  return Error(503, "vector_index_resource_exhausted",
               "Retrieval index exceeds bounded capacity");
}
ObjectReply Active(const VectorSearchControl& control) {
  if (Clock::now() >= control.deadline)
    return Error(408, "request_deadline_exceeded", "Index deadline exceeded");
  if (control.cancelled && control.cancelled())
    return Error(499, "request_cancelled", "Index operation cancelled");
  return {};
}
bool Hex(std::string_view value) {
  return value.size() == 64 &&
         std::all_of(value.begin(), value.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
bool Chunking(const VectorChunking& value) {
  return value.max_tokens >= 100 && value.max_tokens <= 4096 &&
         value.overlap_tokens <= value.max_tokens / 2;
}
bool Identity(const VectorIndexIdentity& identity) {
  return ValidId(identity.file_id) && !identity.filename.empty() &&
         identity.filename.size() <= 1024 &&
         detail::NormalizeUtf8(identity.filename) == identity.filename &&
         Hex(identity.source_sha256) && Hex(identity.text_sha256) &&
         identity.dimensions >= C::kMinimumDimensions &&
         identity.dimensions <= C::kHidden && Chunking(identity.chunking) &&
         identity.backend_sha256 ==
             VectorEmbeddingIdentity(identity.dimensions);
}
Json IdentityJson(const VectorIndexIdentity& identity) {
  return Object(
      {{"file_id", String(identity.file_id)},
       {"filename", String(identity.filename)},
       {"source_sha256", String(identity.source_sha256)},
       {"text_sha256", String(identity.text_sha256)},
       {"backend_sha256", String(identity.backend_sha256)},
       {"dimensions", Number(identity.dimensions)},
       {"max_chunk_size_tokens", Number(identity.chunking.max_tokens)},
       {"chunk_overlap_tokens", Number(identity.chunking.overlap_tokens)}});
}
bool Boundary(std::string_view text, size_t byte) {
  return byte <= text.size() &&
         (byte == text.size() ||
          (static_cast<unsigned char>(text[byte]) & 0xc0) != 0x80);
}
bool Embedding(std::span<const float> values, size_t dimensions) {
  if (values.size() != dimensions) return false;
  double squared = 0;
  for (const auto value : values) {
    if (!std::isfinite(value)) return false;
    squared += double(value) * value;
  }
  return squared > 0 && std::isfinite(squared);
}
ObjectReply Validate(const VectorIndex& index,
                     const VectorSearchControl& control) {
  auto active = Active(control);
  if (!active.ok()) return active;
  if (!Identity(index.identity) || index.text.empty() || index.chunks.empty())
    return Invalid();
  if (index.text.size() > kVectorSearchTextBytes ||
      index.chunks.size() > kVectorSearchChunks ||
      index.identity.dimensions > kVectorSearchValues / index.chunks.size())
    return Capacity();
  if (detail::NormalizeUtf8(index.text) != index.text ||
      Digest(index.text) != index.identity.text_sha256)
    return Invalid();
  uint64_t tokens = 0;
  VectorTokenRange previous;
  for (size_t i = 0; i < index.chunks.size(); ++i) {
    active = Active(control);
    if (!active.ok()) return active;
    const auto& chunk = index.chunks[i];
    const auto& range = chunk.range;
    if (range.token_begin >= range.token_end || range.token_end > kRawTokens ||
        range.token_end - range.token_begin >
            index.identity.chunking.max_tokens ||
        range.byte_begin >= range.byte_end ||
        range.byte_end > index.text.size() ||
        !Boundary(index.text, range.byte_begin) ||
        !Boundary(index.text, range.byte_end) ||
        !Embedding(chunk.embedding, index.identity.dimensions))
      return Invalid();
    if (i == 0) {
      if (range.token_begin || range.byte_begin) return Invalid();
    } else if (range.token_begin <= previous.token_begin ||
               range.token_begin > previous.token_end ||
               previous.token_end - range.token_begin >
                   index.identity.chunking.overlap_tokens ||
               range.token_end <= previous.token_end ||
               range.byte_begin <= previous.byte_begin ||
               range.byte_begin > previous.byte_end ||
               range.byte_end <= previous.byte_end ||
               ((range.token_begin == previous.token_end) !=
                (range.byte_begin == previous.byte_end))) {
      return Invalid();
    }
    tokens += range.token_end - range.token_begin + 1;  // Actual appended EOS.
    previous = range;
  }
  return previous.byte_end == index.text.size() && tokens == index.input_tokens
             ? Active(control)
             : Invalid();
}
bool Integer(const Json& value, const std::string& key, uint64_t max,
             size_t* output) {
  const auto* raw = value.Find(key);
  if (!raw || !IsInputInteger(*raw) || raw->number < 0 || raw->number > max)
    return false;
  *output = raw->AsInt();
  return true;
}
}  // namespace

std::string VectorEmbeddingIdentity(size_t dimensions) {
  if (dimensions < C::kMinimumDimensions || dimensions > C::kHidden) return {};
  return Digest(std::string("q4t.vector_embedding.v1:") +
                kNativeEmbeddingRevision + ':' + kNativeEmbeddingWeightSha256 +
                ':' + kNativeEmbeddingTokenizerSha256 +
                ":raw_eos_last_l2:" + std::to_string(dimensions));
}
std::string VectorIndexBinding(const VectorIndexIdentity& identity) {
  if (!Identity(identity)) return {};
  std::string result;
  if (!InputValueIdentity(Object({{"format", String("q4t.vector_index.v1")},
                                  {"identity", IdentityJson(identity)}}),
                          &result))
    return {};
  return result;
}
ObjectReply PlanVectorTokenChunks(std::string_view text,
                                  std::span<const size_t> boundaries,
                                  const VectorChunking& chunking,
                                  const VectorSearchControl& control,
                                  std::vector<VectorTokenRange>* chunks) {
  chunks->clear();
  auto active = Active(control);
  if (!active.ok()) return active;
  if (text.size() > kVectorSearchTextBytes ||
      boundaries.size() > kRawTokens + 1)
    return Capacity();
  if (!Chunking(chunking) || text.empty() || boundaries.size() < 2 ||
      boundaries.front() != 0 || boundaries.back() != text.size() ||
      detail::NormalizeUtf8(text) != text)
    return Invalid();
  for (size_t i = 1; i < boundaries.size(); ++i) {
    if (i % 4096 == 0) {
      active = Active(control);
      if (!active.ok()) return active;
    }
    if (boundaries[i] <= boundaries[i - 1] || boundaries[i] > text.size())
      return Invalid();
  }
  std::vector<VectorTokenRange> candidate;
  const size_t tokens = boundaries.size() - 1;
  for (size_t begin = 0; begin < tokens;) {
    active = Active(control);
    if (!active.ok()) return active;
    size_t end = std::min(tokens, begin + chunking.max_tokens);
    while (end > begin && !Boundary(text, boundaries[end])) --end;
    if (end == begin) return Invalid();
    candidate.push_back({begin, end, boundaries[begin], boundaries[end]});
    if (candidate.size() > kVectorSearchChunks) return Capacity();
    if (end == tokens) break;
    size_t next = std::max(
        begin + 1, end - std::min(end - begin - 1, chunking.overlap_tokens));
    while (next < end && !Boundary(text, boundaries[next])) ++next;
    begin = next;
  }
  active = Active(control);
  if (!active.ok()) return active;
  *chunks = std::move(candidate);
  return {};
}
ObjectReply BuildVectorIndex(const VectorIndexIdentity& identity,
                             std::string_view text,
                             std::span<const uint32_t> tokens,
                             std::span<const size_t> boundaries,
                             const VectorIndexEmbedding& embed,
                             const VectorSearchControl& control,
                             VectorIndex* index, uint64_t* committed_input) {
  *index = {};
  *committed_input = 0;
  if (!Identity(identity) || !embed || tokens.size() + 1 != boundaries.size())
    return Invalid();
  if (text.size() > kVectorSearchTextBytes || tokens.size() > kRawTokens)
    return Capacity();
  if (Digest(text) != identity.text_sha256) return Invalid();
  for (size_t i = 0; i < tokens.size(); ++i) {
    if (i % 4096 == 0) {
      auto active = Active(control);
      if (!active.ok()) return active;
    }
    if (tokens[i] >= C::kVocab) return Invalid();
  }
  std::vector<VectorTokenRange> ranges;
  auto reply = PlanVectorTokenChunks(text, boundaries, identity.chunking,
                                     control, &ranges);
  if (!reply.ok()) return reply;
  if (identity.dimensions > kVectorSearchValues / ranges.size())
    return Capacity();
  VectorIndex candidate;
  candidate.identity = identity;
  candidate.text = text;
  for (const auto& range : ranges) {
    reply = Active(control);
    if (!reply.ok()) return reply;
    std::vector<uint32_t> input(tokens.begin() + range.token_begin,
                                tokens.begin() + range.token_end);
    input.push_back(C::kEos);
    VectorIndexChunk chunk{range, {}};
    uint64_t used = 0;
    try {
      reply = embed(input, &chunk.embedding, &used);
    } catch (...) {
      if (used <= UINT64_MAX - *committed_input) *committed_input += used;
      throw;
    }
    // The trusted executor owns actual accounting. Even a failed or malformed
    // result cannot erase work already consumed by the backend.
    if (used > UINT64_MAX - *committed_input) return Invalid();
    *committed_input += used;
    if (!reply.ok()) return reply;
    if (used != input.size() ||
        !Embedding(chunk.embedding, identity.dimensions))
      return Invalid();
    candidate.chunks.push_back(std::move(chunk));
  }
  candidate.input_tokens = *committed_input;
  reply = Validate(candidate, control);
  if (!reply.ok()) return reply;
  *index = std::move(candidate);
  return {};
}
ObjectReply SaveVectorIndex(InputSnapshotStore& snapshots,
                            const BinaryAccess& access,
                            const VectorIndex& index, int hold_seconds,
                            InputSnapshotRef* reference,
                            InputSnapshotHold* hold) {
  const VectorSearchControl control{access.deadline, access.cancelled};
  auto valid = Validate(index, control);
  if (!valid.ok()) return valid;
  auto chunks = Array();
  std::string vectors;
  vectors.reserve(index.chunks.size() * index.identity.dimensions * 4);
  for (const auto& chunk : index.chunks) {
    valid = Active(control);
    if (!valid.ok()) return valid;
    const auto& range = chunk.range;
    chunks.array.push_back(Object({{"token_begin", Number(range.token_begin)},
                                   {"token_end", Number(range.token_end)},
                                   {"byte_begin", Number(range.byte_begin)},
                                   {"byte_end", Number(range.byte_end)}}));
    for (const float value : chunk.embedding) {
      const uint32_t bits = std::bit_cast<uint32_t>(value);
      for (unsigned shift = 0; shift < 32; shift += 8)
        vectors += static_cast<char>((bits >> shift) & 255);
    }
  }
  const auto metadata = Object({{"format", String("q4t.vector_index.v1")},
                                {"identity", IdentityJson(index.identity)},
                                {"input_tokens", Number(index.input_tokens)},
                                {"chunks", std::move(chunks)}});
  const std::array<std::string_view, 2> parts{index.text, vectors};
  return snapshots.Save(access, VectorIndexBinding(index.identity), metadata,
                        parts, kStorage, hold_seconds, reference, hold);
}
ObjectReply ReadVectorIndex(InputSnapshotStore& snapshots,
                            const BinaryAccess& access,
                            const InputSnapshotRef& reference,
                            const VectorIndexIdentity& expected,
                            VectorIndex* index) {
  *index = {};
  if (!Identity(expected)) return Invalid();
  InputSnapshot snapshot;
  const auto binding = VectorIndexBinding(expected);
  auto reply = snapshots.Read(access, reference, binding, kStorage, &snapshot);
  if (!reply.ok()) return reply;
  const auto& metadata = snapshot.metadata;
  std::string expected_identity, stored_identity;
  const auto* identity = metadata.Find("identity");
  const auto* chunks = metadata.GetArray("chunks");
  if (snapshot.parts.size() != 2 || metadata.object.size() != 4 ||
      metadata.GetString("format") != "q4t.vector_index.v1" || !identity ||
      !InputValueIdentity(IdentityJson(expected), &expected_identity) ||
      !InputValueIdentity(*identity, &stored_identity) ||
      expected_identity != stored_identity || !chunks || chunks->array.empty())
    return Invalid();
  if (chunks->array.size() > kVectorSearchChunks ||
      expected.dimensions > kVectorSearchValues / chunks->array.size())
    return Capacity();
  const auto& vectors = snapshot.parts[1];
  if (vectors.size() != chunks->array.size() * expected.dimensions * 4)
    return Invalid();
  VectorIndex candidate;
  candidate.identity = expected;
  candidate.text = std::move(snapshot.parts[0]);
  size_t used = 0;
  if (!Integer(metadata, "input_tokens", kVectorSearchChunks * 4097, &used))
    return Invalid();
  candidate.input_tokens = used;
  size_t offset = 0;
  for (const auto& value : chunks->array) {
    reply = Active({access.deadline, access.cancelled});
    if (!reply.ok()) return reply;
    VectorIndexChunk chunk;
    auto& range = chunk.range;
    if (!value.IsObject() || value.object.size() != 4 ||
        !Integer(value, "token_begin", kRawTokens, &range.token_begin) ||
        !Integer(value, "token_end", kRawTokens, &range.token_end) ||
        !Integer(value, "byte_begin", candidate.text.size(),
                 &range.byte_begin) ||
        !Integer(value, "byte_end", candidate.text.size(), &range.byte_end))
      return Invalid();
    chunk.embedding.resize(expected.dimensions);
    for (auto& item : chunk.embedding) {
      uint32_t bits = 0;
      for (unsigned shift = 0; shift < 32; shift += 8)
        bits |= uint32_t(static_cast<unsigned char>(vectors[offset++]))
                << shift;
      item = std::bit_cast<float>(bits);
    }
    candidate.chunks.push_back(std::move(chunk));
  }
  reply = Validate(candidate, {access.deadline, access.cancelled});
  if (!reply.ok()) return reply;
  *index = std::move(candidate);
  return {};
}
ObjectReply VectorIndexSearchChunks(const VectorIndex& index,
                                    const Json& attributes,
                                    const VectorSearchControl& control,
                                    std::vector<VectorSearchChunk>* chunks) {
  chunks->clear();
  auto valid = Validate(index, control);
  if (!valid.ok()) return valid;
  ChatPlanError error;
  if (!ValidateVectorAttributes(attributes, &error)) return Invalid();
  const auto binding = VectorIndexBinding(index.identity);
  std::vector<VectorSearchChunk> candidate;
  size_t metadata_bytes =
      index.identity.filename.size() + index.identity.file_id.size() + 64;
  for (const auto& [key, value] : attributes.object)
    metadata_bytes += key.size() + value.str.size() + 64;
  size_t remaining = kVectorSearchTextBytes;
  for (const auto& chunk : index.chunks) {
    valid = Active(control);
    if (!valid.ok()) return valid;
    const auto& range = chunk.range;
    const auto bytes = range.byte_end - range.byte_begin + metadata_bytes;
    if (bytes > remaining) return Capacity();
    remaining -= bytes;
    const auto id = Digest(binding + ':' + std::to_string(range.token_begin) +
                           ':' + std::to_string(range.token_end));
    candidate.push_back(
        {index.identity.file_id, index.identity.filename, id,
         index.text.substr(range.byte_begin, range.byte_end - range.byte_begin),
         attributes, chunk.embedding});
  }
  valid = Active(control);
  if (!valid.ok()) return valid;
  *chunks = std::move(candidate);
  return {};
}
}  // namespace q4t::server
