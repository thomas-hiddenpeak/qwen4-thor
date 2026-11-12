#include "q4t/server/vector_stores.h"

#include <ctime>

#include "q4t/server/files.h"
#include "vector_stores_internal.h"

namespace q4t::server {
using namespace vector_store_detail;

ObjectReply VectorStores::Read(const BinaryAccess& access,
                               const std::string& id, bool require_live) {
  if (!ValidId(id) || !id.starts_with("vs_"))
    return Invalid("Invalid vector store ID", "vector_store_id");
  auto request = Write(access, ObjectKind::kVectorStore, id, 0, {});
  request.operation = ObjectOperation::kGet;
  auto reply = objects_.Execute(request, access.deadline, access.cancelled);
  if (!reply.ok()) return reply;
  if (!ValidateStore(reply.value)) return Corrupt();
  if (require_live && !Live(*reply.value.Find("data"))) return Expired();
  return reply;
}
ObjectReply VectorStores::ReadMember(const BinaryAccess& access,
                                     const Json& store,
                                     const std::string& file_id) {
  const auto* edge = Edge(*store.Find("data"), file_id);
  if (!edge)
    return Error(404, "vector_store_file_not_found",
                 "File is not in this store");
  auto request =
      Write(access, ObjectKind::kVectorStoreFile, edge->GetString("id"), 0, {});
  request.operation = ObjectOperation::kGet;
  auto reply = objects_.Execute(request, access.deadline, access.cancelled);
  if (!reply.ok()) return reply;
  // Root and member updates are atomic, but these reads may straddle a commit.
  // Return a known revision conflict instead of declaring corruption on a
  // changed root. Callers retry boundedly with a fresh pair.
  if (!ValidateMember(reply.value, store)) {
    auto fresh = Read(access, store.GetString("id"), true);
    if (!fresh.ok()) return fresh;
    if (fresh.value.GetInt("revision") != store.GetInt("revision"))
      return Error(409, "object_revision_conflict", "Vector store changed");
    return Corrupt();
  }
  if (reply.value.Find("data")->GetString("scope") != access.scope)
    return Corrupt();
  return reply;
}
ObjectReply VectorStores::Publish(const BinaryAccess& access,
                                  const Json& before, Json data,
                                  std::span<const ObjectRequest> children) {
  Summarize(&data);
  const auto id = before.GetString("id");
  const auto* previous = before.Find("data");
  const auto deadline =
      previous ? previous->Find("vector_store")->GetInt("expires_at") : 0;
  auto root = Write(access, ObjectKind::kVectorStore, id,
                    before.GetInt("revision"), std::move(data), deadline);
  root.created_at = before.GetInt("created_at");
  std::vector<ObjectRequest> requests;
  requests.reserve(children.size() + 1);
  requests.push_back(std::move(root));
  requests.insert(requests.end(), children.begin(), children.end());
  auto synced = SyncBatches(access, before, requests.front().data, children,
                            &requests);
  if (!synced.ok()) return synced;
  auto reply =
      objects_.ExecuteBatch(requests, access.deadline, access.cancelled);
  if (reply.commit_unknown) {
    reply.value = Object({{"id", String(id)}});
  } else if (reply.ok()) {
    // Use the exact committed draft, not a later read that may race deletion.
    reply.value = PublicStore(requests.front().data);
  }
  return reply;
}
ObjectReply VectorStores::Create(const BinaryAccess& access,
                                 const Json& request) {
  auto valid = Fields(request, true);
  if (!valid.ok()) return valid;
  const auto id = NewObjectId(ObjectKind::kVectorStore);
  if (id.empty())
    return Error(503, "object_id_unavailable", "No vector store ID");
  const auto now = std::time(nullptr);
  auto public_store = Object({{"id", String(id)},
                              {"object", String("vector_store")},
                              {"created_at", Number(now)},
                              {"last_active_at", Number(now)},
                              {"name", String("")},
                              {"description", String("")},
                              {"metadata", Object()},
                              {"expires_after", Json()},
                              {"expires_at", Json()}});
  for (const auto* key : {"name", "description", "metadata", "expires_after"})
    if (const auto* value = request.Find(key)) Set(&public_store, key, *value);
  auto data = Object({{"format", String(std::string(kStore))},
                      {"vector_store", std::move(public_store)},
                      {"members", Array()}});
  Activity(&data);
  std::vector<ObjectRequest> children;
  if (const auto* ids = request.GetArray("file_ids"))
    for (const auto& file_id : ids->array) {
      auto input = Object({{"file_id", file_id}});
      if (const auto* chunking = request.Find("chunking_strategy"))
        Set(&input, "chunking_strategy", *chunking);
      Json child, edge;
      auto reply =
          PrepareMember(objects_, access, id, input, now, &child, &edge);
      if (!reply.ok()) return reply;
      auto write = Write(access, ObjectKind::kVectorStoreFile,
                         edge.GetString("id"), 0, std::move(child));
      write.created_at = now;
      children.push_back(std::move(write));
      Field(data, "members").array.push_back(std::move(edge));
    }
  const auto before = Object({{"id", String(id)}, {"created_at", Number(now)}});
  return Publish(access, before, std::move(data), children);
}
ObjectReply VectorStores::Retrieve(const BinaryAccess& access,
                                   const std::string& id) {
  auto reply = Read(access, id);
  if (reply.ok()) reply.value = PublicStore(*reply.value.Find("data"));
  return reply;
}
ObjectReply VectorStores::Update(const BinaryAccess& access,
                                 const std::string& id, const Json& request) {
  auto valid = Fields(request, false);
  if (!valid.ok()) return valid;
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto reply = Read(access, id);
    if (!reply.ok()) return reply;
    auto data = *reply.value.Find("data");
    const bool live = Live(data);
    // An expired store's members may already have been collected. Updating
    // its retention policy cannot resurrect dangling references.
    if (!live && request.Has("expires_after")) return Expired();
    auto& store = Field(data, "vector_store");
    for (const auto* key : {"name", "metadata", "expires_after"})
      if (const auto* value = request.Find(key)) Set(&store, key, *value);
    if (request.Has("expires_after")) {
      const auto days = store.Find("expires_after")->GetInt("days");
      Set(&store, "expires_at",
          days ? Number(store.GetInt("last_active_at") + days * 86400)
               : Json());
    }
    if (!live) {
      // Retain expired metadata, with no private dependency revalidation.
      auto write = Write(access, ObjectKind::kVectorStore, id,
                         reply.value.GetInt("revision"), std::move(data));
      auto changed = objects_.Execute(write, access.deadline, access.cancelled);
      if (changed.commit_unknown) changed.value = Object({{"id", String(id)}});
      if (changed.ok()) changed.value = PublicStore(write.data);
      if (!Retry(changed)) return changed;
    } else {
      auto changed = Publish(access, reply.value, std::move(data));
      if (!Retry(changed)) return changed;
    }
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}
ObjectReply VectorStores::Erase(const BinaryAccess& access,
                                const std::string& id) {
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto reply = Read(access, id);
    if (!reply.ok()) return reply;
    auto request = Write(access, ObjectKind::kVectorStore, id,
                         reply.value.GetInt("revision"), {});
    request.operation = ObjectOperation::kErase;
    reply = objects_.Execute(request, access.deadline, access.cancelled);
    if (Retry(reply)) continue;
    if (reply.ok())
      reply.value = Object({{"id", String(id)},
                            {"object", String("vector_store.deleted")},
                            {"deleted", Boolean(true)}});
    if (reply.commit_unknown) reply.value = Object({{"id", String(id)}});
    return reply;
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}
ObjectReply VectorStores::List(const BinaryAccess& access, ObjectRequest page) {
  page.operation = ObjectOperation::kList;
  page.kind = ObjectKind::kVectorStore;
  page.scope = access.scope;
  page.file_metadata_only = false;
  auto reply = objects_.Execute(page, access.deadline, access.cancelled);
  if (!reply.ok()) return reply;
  for (auto& row : Field(reply.value, "data").array) {
    if (!ValidateStore(row)) return Corrupt();
    row = PublicStore(*row.Find("data"));
  }
  return reply;
}
ObjectReply VectorStores::Touch(const BinaryAccess& access,
                                const std::string& id) {
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto reply = Read(access, id, true);
    if (!reply.ok()) return reply;
    auto data = *reply.value.Find("data");
    Activity(&data);
    auto changed = Publish(access, reply.value, std::move(data));
    if (!Retry(changed)) return changed;
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}
}  // namespace q4t::server
