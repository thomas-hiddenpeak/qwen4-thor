#include "q4t/server/vector_stores.h"

#include <algorithm>
#include <ctime>

#include "q4t/server/files.h"
#include "q4t/server/http_response.h"
#include "vector_stores_internal.h"

namespace q4t::server {
using namespace vector_store_detail;

ObjectReply VectorStores::Claim(const BinaryAccess& access,
                                const std::string& id,
                                const std::string& file_id, std::string owner,
                                int lifetime_seconds,
                                VectorIngestionClaim* claim) {
  *claim = {};
  if (owner.empty() || owner.size() > 256 || lifetime_seconds < 1 ||
      lifetime_seconds > 3600 || detail::NormalizeUtf8(owner) != owner)
    return Invalid("Invalid trusted ingestion execution limits");
  const auto token = NewObjectId(ObjectKind::kToolRun);
  if (token.empty()) return Error(503, "object_id_unavailable", "No claim ID");
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto root = Read(access, id, true);
    if (!root.ok()) return root;
    auto member = ReadMember(access, root.value, file_id);
    if (Retry(member)) continue;
    if (!member.ok()) return member;
    auto data = *member.value.Find("data");
    auto& job = Field(data, "job");
    if (job.GetString("state") != "queued")
      return Error(409, "vector_ingestion_claim_conflict",
                   "Ingestion is not queued");
    const auto deadline =
        std::min<int64_t>(std::time(nullptr) + lifetime_seconds,
                          job.GetInt("execution_deadline_at"));
    if (deadline <= std::time(nullptr) || job.GetInt("attempts") >= 3) {
      // Queued work has no active executor to drain. Close it atomically;
      // otherwise every recovery scan would rediscover it forever.
      Set(&job, "state", String("terminal"));
      auto& file = Field(data, "file");
      Set(&file, "status", String("failed"));
      Set(&file, "last_error",
          Object(
              {{"code", String("server_error")},
               {"message", String("Ingestion execution budget exhausted")}}));
      auto root_data = *root.value.Find("data");
      for (auto& edge : Field(root_data, "members").array)
        if (edge.GetString("file_id") == file_id)
          Set(&edge, "status", String("failed"));
      auto write = Write(access, ObjectKind::kVectorStoreFile,
                         member.value.GetString("id"),
                         member.value.GetInt("revision"), std::move(data));
      auto reply =
          Publish(access, root.value, std::move(root_data), {&write, 1});
      if (Retry(reply)) continue;
      if (reply.commit_unknown)
        reply.value =
            Object({{"id", String(file_id)}, {"vector_store_id", String(id)}});
      return reply.ok() ? Error(408, "vector_ingestion_deadline",
                                "Ingestion execution budget exhausted")
                        : reply;
    }
    Set(&job, "state", String("running"));
    Set(&job, "token", String(token));
    Set(&job, "owner", String(owner));
    Set(&job, "deadline_at", Number(deadline));
    Set(&job, "attempts", Number(job.GetInt("attempts") + 1));
    auto write = Write(access, ObjectKind::kVectorStoreFile,
                       member.value.GetString("id"),
                       member.value.GetInt("revision"), data, deadline);
    auto reply =
        Publish(access, root.value, *root.value.Find("data"), {&write, 1});
    if (Retry(reply)) continue;
    if (reply.ok() || reply.commit_unknown) {
      *claim = {id,    file_id, member.value.GetString("id"),
                token, owner,   deadline};
      reply.value =
          Object({{"id", String(file_id)}, {"vector_store_id", String(id)}});
    }
    return reply;
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}
ObjectReply VectorStores::LoadClaim(const BinaryAccess& access,
                                    const VectorIngestionClaim& claim) {
  auto reply = LoadFile(access, claim.store_id, claim.file_id);
  if (!reply.ok()) return reply;
  if (reply.value.GetString("id") != claim.member_id)
    return Error(409, "vector_ingestion_claim_conflict",
                 "File membership changed");
  auto matched = MatchClaim(*reply.value.Find("data"), claim, true);
  return matched.ok() ? reply : matched;
}
ObjectReply VectorStores::ChangeJob(const BinaryAccess& access,
                                    const VectorIngestionClaim& claim,
                                    std::string_view action,
                                    const Json& completion) {
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto root = Read(access, claim.store_id, true);
    if (!root.ok()) return root;
    auto member = ReadMember(access, root.value, claim.file_id);
    if (Retry(member)) continue;
    if (!member.ok()) return member;
    if (member.value.GetString("id") != claim.member_id)
      return Error(409, "vector_ingestion_claim_conflict",
                   "File membership changed");
    auto data = *member.value.Find("data");
    auto matched = MatchClaim(data, claim, action == "completed");
    if (!matched.ok()) return matched;
    data.object.reserve(data.object.size() + 2);
    auto& file = Field(data, "file");
    auto& job = Field(data, "job");
    if (action == "queued" && job.GetInt("attempts") < 3 &&
        job.GetInt("execution_deadline_at") > std::time(nullptr)) {
      Set(&job, "state", String("queued"));
      Set(&job, "token", Json());
      Set(&job, "owner", Json());
      Set(&job, "deadline_at", Json());
    } else {
      const auto status = action == "queued" ? "failed" : std::string(action);
      Set(&job, "state", String("terminal"));
      Set(&file, "status", String(status));
      Set(&file, "usage_bytes", Number(completion.GetInt("usage_bytes")));
      if (status == "completed") {
        VectorIndexIdentity identity;
        ChatPlanError error;
        VectorChunking chunking;
        if (!completion.Find("identity") ||
            !ParseVectorIndexIdentity(*completion.Find("identity"),
                                      &identity) ||
            !ParseVectorChunking(*file.Find("chunking_strategy"), &chunking,
                                 &error) ||
            identity.file_id != claim.file_id ||
            identity.filename != data.GetString("filename") ||
            identity.chunking.max_tokens != chunking.max_tokens ||
            identity.chunking.overlap_tokens != chunking.overlap_tokens)
          return Corrupt();
        Set(&data, "index_identity", *completion.Find("identity"));
        Set(&data, "input_snapshots",
            Array({Object(
                {{"snapshot", *completion.Find("snapshot")},
                 {"binding_sha256", String(VectorIndexBinding(identity))}})}));
      } else if (status == "failed") {
        Set(&file, "last_error",
            action == "queued"
                ? Object({{"code", String("server_error")},
                          {"message",
                           String("Ingestion recovery budget exhausted")}})
                : completion);
      }
    }
    auto root_data = *root.value.Find("data");
    for (auto& edge : Field(root_data, "members").array)
      if (edge.GetString("file_id") == claim.file_id) {
        Set(&edge, "status", *file.Find("status"));
        Set(&edge, "usage_bytes", *file.Find("usage_bytes"));
      }
    auto write = Write(access, ObjectKind::kVectorStoreFile, claim.member_id,
                       member.value.GetInt("revision"), data,
                       action == "completed" ? claim.deadline_at : 0);
    auto reply = Publish(access, root.value, std::move(root_data), {&write, 1});
    if (Retry(reply)) continue;
    if (reply.ok()) reply.value = *data.Find("file");
    if (reply.commit_unknown)
      reply.value = Object({{"id", String(claim.file_id)},
                            {"vector_store_id", String(claim.store_id)}});
    return reply;
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}
ObjectReply VectorStores::RequeueAfterExit(const BinaryAccess& access,
                                           const VectorIngestionClaim& claim) {
  return ChangeJob(access, claim, "queued", {});
}
ObjectReply VectorStores::Finish(const BinaryAccess& access,
                                 const VectorIngestionClaim& claim,
                                 const VectorIndexIdentity& identity,
                                 const InputSnapshotRef& reference) {
  // Read verifies exact bytes, backend identity, vectors and chunk boundaries
  // before declaring the durable member ready. No unverified "completed".
  auto live = LoadClaim(access, claim);
  if (!live.ok()) return live;
  InputSnapshotStore snapshots(objects_);
  VectorIndex index;
  auto result = ReadVectorIndex(snapshots, access, reference, identity, &index);
  if (!result.ok()) return result;
  const auto bytes = index.text.size() +
                     index.chunks.size() * identity.dimensions * sizeof(float);
  if (bytes > (64ULL << 20)) return Corrupt();
  return ChangeJob(access, claim, "completed",
                   Object({{"identity", VectorIndexIdentityJson(identity)},
                           {"snapshot", InputSnapshotReference(reference)},
                           {"usage_bytes", Number(bytes)}}));
}
ObjectReply VectorStores::Fail(const BinaryAccess& access,
                               const VectorIngestionClaim& claim,
                               std::string code, std::string message) {
  if ((code != "server_error" && code != "unsupported_file" &&
       code != "invalid_file") ||
      message.empty() || message.size() > 4096 ||
      detail::NormalizeUtf8(message) != message)
    return Invalid("Invalid ingestion failure");
  return ChangeJob(access, claim, "failed",
                   Object({{"code", String(std::move(code))},
                           {"message", String(std::move(message))}}));
}
ObjectReply VectorStores::Cancel(const BinaryAccess& access,
                                 const VectorIngestionClaim& claim) {
  return ChangeJob(access, claim, "cancelled", {});
}
ObjectReply VectorStores::RequestCancel(const BinaryAccess& access,
                                        const std::string& id,
                                        const std::string& file_id) {
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto root = Read(access, id, true);
    if (!root.ok()) return root;
    auto member = ReadMember(access, root.value, file_id);
    if (Retry(member)) continue;
    if (!member.ok()) return member;
    auto data = *member.value.Find("data");
    const auto& file = Field(data, "file");
    if (file.GetString("status") != "in_progress" ||
        Field(data, "job").GetBool("cancel_requested"))
      return Result(file);
    Set(&Field(data, "job"), "cancel_requested", Boolean(true));
    auto write = Write(access, ObjectKind::kVectorStoreFile,
                       member.value.GetString("id"),
                       member.value.GetInt("revision"), data);
    auto reply =
        Publish(access, root.value, *root.value.Find("data"), {&write, 1});
    if (Retry(reply)) continue;
    if (reply.ok()) reply.value = file;
    return reply;
  }
  return Error(409, "object_revision_conflict", "Vector store kept changing");
}
}  // namespace q4t::server
