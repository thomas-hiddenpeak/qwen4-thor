#include "q4t/server/vector_stores.h"

#include <iterator>

#include "q4t/model/embedding_config.h"
#include "q4t/server/files.h"
#include "vector_stores_internal.h"

namespace q4t::server {
using namespace vector_store_detail;
namespace {
ObjectReply Active(const BinaryAccess& access) {
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Search cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded", "Search deadline exceeded");
  return {};
}
ObjectReply Changed() {
  return Error(409, "vector_search_snapshot_changed",
               "Vector store search membership or attributes changed");
}
ObjectReply Capacity() {
  return Error(503, "vector_search_resource_exhausted",
               "Complete search corpus exceeds bounded capacity");
}
}  // namespace

ObjectReply VectorStores::CheckSearch(const BinaryAccess& access,
                                      const VectorSearchCorpus& corpus) {
  if (access.scope != corpus.scope || corpus.search_revision < 1)
    return Error(403, "vector_search_owner_invalid", "Search owner changed");
  auto active = Active(access);
  if (!active.ok()) return active;
  auto root = Read(access, corpus.store_id, true);
  if (!root.ok()) return root;
  if (SearchRevision(root.value) != corpus.search_revision) return Changed();
  for (const auto& file : corpus.file_ids) {
    auto source = Files(objects_).Retrieve(access, file);
    if (!source.ok()) return source;
  }
  return root;
}

ObjectReply VectorStores::ReadSearchCorpus(const BinaryAccess& access,
                                           const std::string& id,
                                           const Json& filters,
                                           size_t dimensions,
                                           VectorSearchCorpus* corpus) {
  *corpus = {};
  ChatPlanError error;
  const auto checked = ValidateVectorFilter(filters, &error);
  if (!checked) return Invalid(checked.message(), error.param);
  if (dimensions < model::EmbeddingConfig::kMinimumDimensions ||
      dimensions > model::EmbeddingConfig::kHidden)
    return Error(503, "vector_search_backend_mismatch",
                 "Search embedding dimensions are not configured");
  auto root = Read(access, id, true);
  if (!root.ok()) return root;
  VectorSearchCorpus result;
  result.store_id = id;
  result.scope = access.scope;
  result.search_revision = SearchRevision(root.value);
  result.dimensions = dimensions;
  size_t values = 0, bytes = 0;
  const VectorSearchControl control{access.deadline, access.cancelled};
  for (const auto& edge : root.value.Find("data")->GetArray("members")->array) {
    auto active = Active(access);
    if (!active.ok()) return active;
    if (edge.GetString("status") != "completed") continue;
    const auto file_id = edge.GetString("file_id");
    auto member = ReadMember(access, root.value, file_id);
    if (!member.ok()) return member;
    const auto& attributes =
        *member.value.Find("data")->Find("file")->Find("attributes");
    bool matches = false;
    auto match = MatchVectorFilter(filters, attributes, &matches, &error);
    if (!match) return Corrupt();
    if (!matches) continue;  // Scope checked before filtering; no index IO yet.
    VectorIndex index;
    Json actual_attributes;
    auto loaded = ReadIndex(access, id, file_id, &index, &actual_attributes);
    if (!loaded.ok()) return loaded;
    if (index.identity.dimensions != dimensions ||
        index.identity.backend_sha256 != VectorEmbeddingIdentity(dimensions))
      return Error(503, "vector_search_backend_mismatch",
                   "Index requires its original embedding backend");
    std::vector<VectorSearchChunk> chunks;
    loaded =
        VectorIndexSearchChunks(index, actual_attributes, control, &chunks);
    if (!loaded.ok()) return loaded;
    if (chunks.size() > kVectorSearchChunks - result.chunks.size())
      return Capacity();
    for (const auto& chunk : chunks) {
      const auto metadata = chunk.file_id.size() + chunk.filename.size() +
                            chunk.chunk_id.size() +
                            OutputJson(chunk.attributes).size();
      if (chunk.embedding.size() > kVectorSearchValues - values ||
          metadata > kVectorSearchTextBytes - bytes)
        return Capacity();
      values += chunk.embedding.size();
      bytes += metadata;
      if (chunk.text.size() > kVectorSearchTextBytes - bytes) return Capacity();
      bytes += chunk.text.size();
    }
    result.file_ids.push_back(file_id);
    result.chunks.insert(result.chunks.end(),
                         std::make_move_iterator(chunks.begin()),
                         std::make_move_iterator(chunks.end()));
  }
  auto valid = CheckSearch(access, result);
  if (!valid.ok()) return valid;
  *corpus = std::move(result);
  return {};
}

ObjectReply VectorStores::TouchSearch(const BinaryAccess& access,
                                      const VectorSearchCorpus& corpus) {
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto current = CheckSearch(access, corpus);
    if (!current.ok()) return current;
    auto data = *current.value.Find("data");
    Activity(&data);
    auto saved = Publish(access, current.value, std::move(data));
    if (Retry(saved)) continue;
    return saved;
  }
  return Changed();
}
}  // namespace q4t::server
