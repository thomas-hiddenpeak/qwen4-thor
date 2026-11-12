#include "q4t/server/vector_stores.h"

#include "q4t/server/files.h"
#include "vector_stores_internal.h"

namespace q4t::server {
using namespace vector_store_detail;

ObjectReply VectorStores::ReadIndex(const BinaryAccess& access,
                                    const std::string& id,
                                    const std::string& file_id,
                                    VectorIndex* index, Json* attributes) {
  *index = {};
  *attributes = {};
  auto root = Read(access, id, true);
  if (!root.ok()) return root;
  auto member = ReadMember(access, root.value, file_id);
  if (!member.ok()) return member;
  const auto& data = *member.value.Find("data");
  const auto& file = *data.Find("file");
  if (file.GetString("status") != "completed")
    return Error(409, "vector_store_file_not_ready",
                 "File ingestion has not completed");
  // Files payloads are immutable; ownership and continued existence are
  // required even though a retained index could outlive source deletion.
  auto source = Files(objects_).Retrieve(access, file_id);
  if (!source.ok()) return source;
  VectorIndexIdentity identity;
  InputSnapshotRef reference;
  const auto* refs = data.GetArray("input_snapshots");
  if (!data.Find("index_identity") ||
      !ParseVectorIndexIdentity(*data.Find("index_identity"), &identity) ||
      !refs || refs->array.size() != 1 ||
      !refs->array.front().Find("snapshot") ||
      !ParseInputSnapshotReference(*refs->array.front().Find("snapshot"),
                                   &reference))
    return Corrupt();
  InputSnapshotStore snapshots(objects_);
  VectorIndex candidate;
  auto reply =
      ReadVectorIndex(snapshots, access, reference, identity, &candidate);
  if (!reply.ok()) return reply;
  auto final = Read(access, id, true);
  if (!final.ok()) return final;
  if (final.value.GetInt("revision") != root.value.GetInt("revision"))
    return Error(409, "object_revision_conflict",
                 "Vector store changed during index read");
  source = Files(objects_).Retrieve(access, file_id);
  if (!source.ok()) return source;
  *attributes = *file.Find("attributes");
  *index = std::move(candidate);
  return {};
}

ObjectReply VectorStores::FileContent(const BinaryAccess& access,
                                      const std::string& id,
                                      const std::string& file_id) {
  VectorIndex index;
  Json attributes;
  auto reply = ReadIndex(access, id, file_id, &index, &attributes);
  if (!reply.ok()) return reply;
  // Return the actual canonical parsed text once, not duplicated overlap or
  // generated summaries. Full content is bounded by the immutable index.
  return Result(Object(
      {{"object", String("vector_store.file_content.page")},
       {"data", Array({Object({{"type", String("text")},
                               {"text", String(std::move(index.text))}})})},
       {"has_more", Boolean(false)},
       {"next_page", Json()}}));
}
}  // namespace q4t::server
