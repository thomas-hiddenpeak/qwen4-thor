#include "q4t/server/vector_stores.h"

#include <algorithm>
#include <ctime>

#include "q4t/server/files.h"
#include "q4t/server/input_contract.h"
#include "vector_stores_internal.h"

namespace q4t::server {
using namespace vector_store_detail;

ObjectReply VectorStores::Attach(const BinaryAccess& access,
                                 const std::string& id, const Json& request) {
  auto initial = Read(access, id, true);
  if (!initial.ok()) return initial;
  Json child, edge;
  const auto now = std::time(nullptr);
  auto prepared =
      PrepareMember(objects_, access, id, request, now, &child, &edge);
  if (!prepared.ok()) return prepared;
  const auto file_id = edge.GetString("file_id");
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto root = attempt ? Read(access, id, true) : std::move(initial);
    if (!root.ok()) return root;
    auto data = *root.value.Find("data");
    if (Edge(data, file_id))
      return Error(409, "vector_store_file_exists", "File is already attached");
    auto& members = Field(data, "members");
    if (members.array.size() >= kVectorStoreFiles)
      return Error(409, "vector_store_capacity",
                   "Vector store file limit reached");
    members.array.push_back(edge);
    auto write = Write(access, ObjectKind::kVectorStoreFile,
                       edge.GetString("id"), 0, child);
    write.created_at = now;
    auto reply = Publish(access, root.value, std::move(data), {&write, 1});
    if (Retry(reply)) continue;
    if (reply.ok()) reply.value = *child.Find("file");
    if (reply.commit_unknown)
      reply.value =
          Object({{"id", String(file_id)}, {"vector_store_id", String(id)}});
    return reply;
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}
ObjectReply VectorStores::LoadRecord(const BinaryAccess& access,
                                   const std::string& id,
                                   const std::string& file_id) {
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto root = Read(access, id, true);
    if (!root.ok()) return root;
    auto reply = ReadMember(access, root.value, file_id);
    if (Retry(reply)) continue;
    if (!reply.ok()) return reply;
    return reply;
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}
ObjectReply VectorStores::LoadFile(const BinaryAccess& access,
                                   const std::string& id,
                                   const std::string& file_id) {
  auto reply = LoadRecord(access, id, file_id);
  if (!reply.ok()) return reply;
  auto source = Files(objects_).Retrieve(access, file_id);
  return source.ok() ? reply : source;
}
ObjectReply VectorStores::RetrieveFile(const BinaryAccess& access,
                                       const std::string& id,
                                       const std::string& file_id) {
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto root = Read(access, id, true);
    if (!root.ok()) return root;
    auto reply = ReadMember(access, root.value, file_id);
    if (Retry(reply)) continue;
    if (reply.ok()) reply.value = *reply.value.Find("data")->Find("file");
    return reply;
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}
ObjectReply VectorStores::UpdateFile(const BinaryAccess& access,
                                     const std::string& id,
                                     const std::string& file_id,
                                     const Json& request) {
  if (!InputFields(request, {"attributes"}) || !request.Has("attributes"))
    return Invalid("Expected attributes", "attributes");
  ChatPlanError error;
  if (!ValidateVectorAttributes(*request.Find("attributes"), &error))
    return Invalid("Invalid attributes", error.param);
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto root = Read(access, id, true);
    if (!root.ok()) return root;
    auto member = ReadMember(access, root.value, file_id);
    if (Retry(member)) continue;
    if (!member.ok()) return member;
    auto data = *member.value.Find("data");
    Set(&Field(data, "file"), "attributes", *request.Find("attributes"));
    auto write = Write(access, ObjectKind::kVectorStoreFile,
                       member.value.GetString("id"),
                       member.value.GetInt("revision"), data);
    auto reply =
        Publish(access, root.value, *root.value.Find("data"), {&write, 1});
    if (Retry(reply)) continue;
    if (reply.ok()) reply.value = *data.Find("file");
    if (reply.commit_unknown)
      reply.value =
          Object({{"id", String(file_id)}, {"vector_store_id", String(id)}});
    return reply;
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}
ObjectReply VectorStores::Detach(const BinaryAccess& access,
                                 const std::string& id,
                                 const std::string& file_id) {
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto root = Read(access, id, true);
    if (!root.ok()) return root;
    auto data = *root.value.Find("data");
    const auto* edge = Edge(data, file_id);
    if (!edge)
      return Error(404, "vector_store_file_not_found",
                   "File is not in this store");
    std::vector<ObjectRequest> closing;
    if (!edge->GetString("batch_id").empty() &&
        edge->GetString("status") == "in_progress") {
      auto member = ReadMember(access, root.value, file_id);
      if (Retry(member)) continue;
      if (!member.ok()) return member;
      auto child = *member.value.Find("data");
      auto& job = Field(child, "job");
      Set(&job, "cancel_requested", Boolean(true));
      if (job.GetString("state") == "queued") {
        Set(&job, "state", String("terminal"));
        Set(&Field(child, "file"), "status", String("cancelled"));
      }
      Set(&child, "detached", Boolean(true));
      closing.push_back(Write(access, ObjectKind::kVectorStoreFile,
                               member.value.GetString("id"),
                               member.value.GetInt("revision"), std::move(child)));
    }
    // Unlink first; GC later reclaims the private member and its snapshot.
    // A detached execution cannot publish because it must CAS this root and
    // match the random member identity, even if the same file is reattached.
    std::erase_if(Field(data, "members").array, [&](const auto& edge) {
      return edge.GetString("file_id") == file_id;
    });
    auto reply = Publish(access, root.value, std::move(data), closing);
    if (Retry(reply)) continue;
    if (reply.ok())
      reply.value = Object({{"id", String(file_id)},
                            {"object", String("vector_store.file.deleted")},
                            {"deleted", Boolean(true)}});
    if (reply.commit_unknown)
      reply.value =
          Object({{"id", String(file_id)}, {"vector_store_id", String(id)}});
    return reply;
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}
ObjectReply VectorStores::ListFiles(const BinaryAccess& access,
                                    const std::string& id, const Json& page) {
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto root = Read(access, id, true);
    if (!root.ok()) return root;
    std::vector<Json> rows;
    bool more = false;
    auto selected = SelectFilePage(
        root.value.Find("data")->GetArray("members")->array, page, &rows, &more);
    if (!selected.ok()) return selected;
    auto items = Array();
    bool retry = false;
    for (const auto& row : rows) {
      auto member = ReadMember(access, root.value, row.GetString("file_id"));
      if (Retry(member)) { retry = true; break; }
      if (!member.ok()) return member;
      items.array.push_back(*member.value.Find("data")->Find("file"));
    }
    if (retry) continue;
    auto final = Read(access, id, true);
    if (!final.ok()) return final;
    if (final.value.GetInt("revision") != root.value.GetInt("revision"))
      continue;
    return FilePage(std::move(items), more);
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}

}  // namespace q4t::server
