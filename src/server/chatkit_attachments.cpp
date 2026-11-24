#include "chatkit_attachments.h"

#include <algorithm>
#include <ctime>
#include <unordered_set>

#include "object_protocol.h"
#include "q4t/server/file_limits.h"

namespace q4t::server::chatkit_detail {
namespace {
using namespace object_detail;

ObjectReply Missing() {
  return Error(404, "object_not_found", "Attachment not found");
}

ObjectReply LiveSession(ObjectStore& objects, const StateAccess& access,
                        const Grant& grant, ObjectRequest* write) {
  ObjectRequest read;
  read.kind = ObjectKind::kChatKitSession;
  read.scope = access.scope;
  read.id = grant.session;
  auto found = objects.Execute(read, access.deadline, access.cancelled);
  if (!found.ok()) return found;
  auto data = Field(found.value, "data");
  const auto* session = data.Find("session");
  if (!session || session->GetString("status") != "active" ||
      session->GetInt("expires_at") <= std::time(nullptr))
    return Error(401, "invalid_client_secret", "Session is no longer active");
  const auto expires = session->GetInt("expires_at");
  if (!data.Has("attachments")) Set(&data, "attachments", Array());
  if (!data.GetArray("attachments"))
    return Error(503, "chatkit_record_invalid",
                 "Invalid session attachment list");
  read.operation = ObjectOperation::kPut;
  read.expected_revision = found.value.GetInt("revision");
  read.expires_at = found.value.GetInt("expires_at");
  read.mutate_before = expires;
  read.data = std::move(data);
  *write = std::move(read);
  return {};
}

const Json& UploadSettings(const Grant& grant) {
  return *grant.data.Find("session")
              ->Find("chatkit_configuration")
              ->Find("file_upload");
}

ObjectRequest File(const StateAccess& access, const std::string& id) {
  ObjectRequest request;
  request.scope = access.scope;
  request.kind = ObjectKind::kFile;
  request.id = id;
  return request;
}
}  // namespace

ObjectReply AttachmentLimit(const Grant& grant, uint64_t* limit) {
  const auto& settings = UploadSettings(grant);
  if (!settings.GetBool("enabled"))
    return Error(403, "chatkit_upload_disabled",
                 "File upload is disabled for this session");
  const auto megabytes = settings.GetInt("max_file_size");
  if (megabytes < 1 || megabytes > 512 || settings.GetInt("max_files") < 1)
    return Error(503, "chatkit_record_invalid",
                 "Invalid session upload settings");
  *limit = static_cast<uint64_t>(megabytes) << 20;
  return {};
}

ObjectReply PublishAttachment(ObjectStore& objects, const BinaryAccess& access,
                              const Grant& grant, BinaryStage* stage,
                              std::string_view name, std::string_view mime) {
  uint64_t limit = 0;
  auto enabled = AttachmentLimit(grant, &limit);
  if (!enabled.ok()) return enabled;
  if (!stage || name.empty())
    return Error(400, "invalid_request", "Upload a file in this session");
  if (stage->manifest.bytes > limit)
    return Error(413, "chatkit_attachment_too_large",
                 "Attachment exceeds the session file size");
  const StateAccess state{access.scope, access.deadline, access.cancelled};
  const auto id = NewObjectId(ObjectKind::kFile);
  if (id.empty())
    return Error(503, "object_entropy_unavailable", "No attachment ID");
  const auto type =
      mime.empty() ? "application/octet-stream" : std::string(mime);
  auto resource = Object({{"id", String(id)},
                          {"name", String(std::string(name))},
                          {"mime_type", String(type)},
                          {"type", String("file")}});
  BinaryStore binary(objects);
  for (int attempt = 0; attempt < 4; ++attempt) {
    ObjectRequest session;
    auto live = LiveSession(objects, state, grant, &session);
    if (!live.ok()) return live;
    auto& ids = Field(session.data, "attachments").array;
    if (ids.size() >=
        static_cast<uint64_t>(UploadSettings(grant).GetInt("max_files")))
      return Error(409, "chatkit_attachment_limit",
                   "Session file count reached");
    ids.push_back(String(id));
    auto file = File(state, id);
    file.operation = ObjectOperation::kPut;
    file.created_at = std::time(nullptr);
    file.expires_at = file.mutate_before = session.mutate_before;
    auto metadata = Object({{"id", String(id)},
                            {"object", String("file")},
                            {"bytes", Number(stage->manifest.bytes)},
                            {"created_at", Number(file.created_at)},
                            {"expires_at", Number(file.expires_at)},
                            {"filename", String(std::string(name))},
                            {"purpose", String("user_data")},
                            {"status", String("processed")}});
    file.data = Object({{"format", String("q4t.file.v1")},
                        {"file", std::move(metadata)},
                        {"mime_type", String(type)},
                        {"chatkit_session", String(grant.session)},
                        {"chatkit_attachment", resource}});
    auto written =
        binary.Publish(access, stage, std::move(file), {&session, 1});
    if (written.ok()) {
      written.value = std::move(resource);
      return written;
    }
    if (written.commit_unknown || written.code != "object_revision_conflict")
      return written;
  }
  return Error(409, "chatkit_session_busy",
               "Session changed during file publication");
}

ObjectReply RemoveAttachment(ObjectStore& objects, const StateAccess& access,
                             const Grant& grant, const std::string& id) {
  if (!id.starts_with("file_") || !ValidApplicationId(id)) return Missing();
  for (int attempt = 0; attempt < 4; ++attempt) {
    ObjectRequest session;
    auto live = LiveSession(objects, access, grant, &session);
    if (!live.ok()) return live;
    auto& ids = Field(session.data, "attachments").array;
    if (!std::erase_if(
            ids, [&](const Json& v) { return v.IsString() && v.str == id; }))
      return Missing();
    auto file = File(access, id);
    auto found = objects.Execute(file, access.deadline, access.cancelled);
    if (!found.ok() && found.http_status != 404) return found;
    std::vector<ObjectRequest> writes;
    writes.push_back(std::move(session));
    if (found.ok()) {
      if (Field(found.value, "data").GetString("chatkit_session") !=
          grant.session)
        return Missing();
      file.operation = ObjectOperation::kErase;
      file.expected_revision = found.value.GetInt("revision");
      writes.push_back(std::move(file));
    }
    auto removed =
        objects.ExecuteBatch(writes, access.deadline, access.cancelled);
    if (removed.ok()) {
      removed.value = Object();
      return removed;
    }
    if (removed.commit_unknown || removed.code != "object_revision_conflict")
      return removed;
  }
  return Error(409, "chatkit_session_busy",
               "Session changed during attachment removal");
}

ObjectReply ResolveAttachments(ObjectStore& objects, const StateAccess& access,
                               const Grant& grant, const Json& ids,
                               Json* published, Json* model) {
  if (ids.array.empty()) return {};
  uint64_t limit = 0;
  auto enabled = AttachmentLimit(grant, &limit);
  if (!enabled.ok()) return enabled;
  if (ids.array.size() >
      static_cast<uint64_t>(UploadSettings(grant).GetInt("max_files")))
    return Error(400, "chatkit_attachment_limit",
                 "Too many message attachments");
  std::unordered_set<std::string> seen;
  for (const auto& id : ids.array) {
    if (!id.IsString() || !id.str.starts_with("file_") ||
        !ValidApplicationId(id.str))
      return Missing();
    if (!seen.insert(id.str).second)
      return Error(400, "invalid_request", "Duplicate attachment");
    auto loaded = objects.Execute(File(access, id.str), access.deadline,
                                  access.cancelled);
    if (!loaded.ok()) return loaded;
    const auto& data = Field(loaded.value, "data");
    if (data.GetString("chatkit_session") != grant.session) return Missing();
    const auto* attachment = data.Find("chatkit_attachment");
    if (!attachment || attachment->GetString("id") != id.str)
      return Error(503, "chatkit_record_invalid", "Invalid stored attachment");
    auto metadata = *attachment;
    Set(&metadata, "preview_url", {});
    published->array.push_back(std::move(metadata));
    model->array.push_back(
        Object({{"type", String("input_file")}, {"file_id", id}}));
  }
  return {};
}
}  // namespace q4t::server::chatkit_detail
