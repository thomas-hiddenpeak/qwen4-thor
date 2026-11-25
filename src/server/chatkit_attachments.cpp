#include "chatkit_attachments.h"

#include <algorithm>
#include <ctime>
#include <unordered_set>

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/file_image.h"
#include "q4t/server/file_limits.h"
#include "q4t/server/http_framing.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/realtime_client_ticket.h"

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
  if (!data.Has("pending_attachments"))
    Set(&data, "pending_attachments", Object());
  auto& pending = Field(data, "pending_attachments");
  if (!pending.IsObject())
    return Error(503, "chatkit_record_invalid", "Invalid pending attachments");
  const auto now = std::time(nullptr);
  auto& ids = Field(data, "attachments").array;
  for (const auto& [id, reservation] : pending.object) {
    if (reservation.GetInt("expires_at") <= now)
      std::erase_if(ids, [&](const Json& value) {
        return value.IsString() && value.str == id;
      });
  }
  std::erase_if(pending.object, [now](const auto& entry) {
    return entry.second.GetInt("expires_at") <= now;
  });
  read.operation = ObjectOperation::kPut;
  read.expected_revision = found.value.GetInt("revision");
  read.expires_at = found.value.GetInt("expires_at");
  read.mutate_before = expires;
  read.data = std::move(data);
  *write = std::move(read);
  return {};
}

const Json& UploadSettings(const Grant& grant) {
  static const Json empty;
  const auto* session = grant.data.Find("session");
  const auto* configuration =
      session ? session->Find("chatkit_configuration") : nullptr;
  const auto* upload =
      configuration ? configuration->Find("file_upload") : nullptr;
  return upload ? *upload : empty;
}

ObjectRequest File(const StateAccess& access, const std::string& id) {
  ObjectRequest request;
  request.scope = access.scope;
  request.kind = ObjectKind::kFile;
  request.id = id;
  return request;
}

Json Attachment(const std::string& id, std::string_view name,
                std::string_view mime) {
  return Object({{"id", String(id)},
                 {"name", String(std::string(name))},
                 {"mime_type", String(std::string(mime))},
                 {"type", String("file")}});
}

ObjectReply IssueTicket(const StateAccess& access, ChatKitIssuer issuer,
                        ClientTicketAudience audience, int64_t expires,
                        std::string_view prefix, std::string* token) {
  CryptoKey entropy;
  if (CryptoRandom(entropy) != CryptoResult::kOk)
    return Error(503, "object_entropy_unavailable", "No attachment entropy");
  *token = std::string(prefix) + trace::HexDigest(entropy);
  CryptoWipe(entropy);
  if (access.scope == "local:anonymous") return {};
  std::string signed_token;
  const bool issued =
      access.scope.starts_with("client:") &&
      SignRealtimeTicket(issuer.binding, issuer.key_digest,
                         std::string_view(access.scope).substr(7), *token,
                         expires, &signed_token, audience);
  CryptoWipe(token);
  if (!issued)
    return Error(503, "chatkit_issuer_unavailable",
                 "Attachment issuer unavailable");
  *token = std::move(signed_token);
  return {};
}

ObjectReply CheckReservation(const ObjectRequest& session, std::string_view id,
                             std::string_view token, ReservedFile* file) {
  const auto& pending = *session.data.Find("pending_attachments");
  const auto* item = pending.Find(std::string(id));
  if (!item || !token.starts_with("ckup_") ||
      item->GetString("token_sha256") != Digest(token))
    return Error(401, "invalid_upload_token",
                 "Invalid or expired attachment upload");
  if (item->GetInt("expires_at") <= std::time(nullptr))
    return Error(401, "invalid_upload_token", "Attachment upload expired");
  const auto size = item->GetInt("size", -1);
  if (size < 0 || static_cast<uint64_t>(size) > kFileBytes ||
      item->GetString("name").empty() || item->GetString("mime_type").empty())
    return Error(503, "chatkit_record_invalid",
                 "Invalid attachment reservation");
  *file = {item->GetString("name"), item->GetString("mime_type"),
           static_cast<uint64_t>(size)};
  return {};
}
}  // namespace

ObjectReply AttachmentLimit(const Grant& grant, uint64_t* limit) {
  const auto& settings = UploadSettings(grant);
  if (!settings.IsObject())
    return Error(503, "chatkit_record_invalid", "Missing upload settings");
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

ObjectReply ReserveAttachment(ObjectStore& objects, const StateAccess& access,
                              const Grant& grant, const Json& params,
                              ChatKitIssuer issuer, std::string_view base) {
  uint64_t limit = 0;
  auto enabled = AttachmentLimit(grant, &limit);
  if (!enabled.ok()) return enabled;
  const auto name = params.GetString("name");
  const auto* size = params.Find("size");
  const auto* type = params.Find("mime_type");
  if (name.empty() || name.size() > 1024 || name.find('\0') != name.npos ||
      detail::NormalizeUtf8(name) != name || !size || !IsInputInteger(*size) ||
      size->number < 0 || !type || !type->IsString())
    return Error(400, "invalid_request",
                 "Expected a filename, size and media type");
  if (size->number > static_cast<double>(limit))
    return Error(413, "chatkit_attachment_too_large",
                 "Attachment exceeds the session file size");
  const auto mime =
      type->str.empty() ? std::string("application/octet-stream") : type->str;
  if (mime.size() > 255 || std::ranges::any_of(mime, [](unsigned char ch) {
        return ch < 0x20 || ch > 0x7e;
      }))
    return Error(400, "invalid_request", "Invalid attachment media type");
  if (base.empty())
    return Error(400, "invalid_upload_origin",
                 "Invalid attachment upload origin");
  const auto id = NewObjectId(ObjectKind::kFile);
  if (id.empty())
    return Error(503, "object_entropy_unavailable", "No attachment ID");
  const auto session_expires = grant.data.Find("session")->GetInt("expires_at");
  const auto expires =
      std::min<int64_t>(std::time(nullptr) + 300, session_expires);
  std::string token;
  auto issued =
      IssueTicket(access, issuer, ClientTicketAudience::kChatKitUpload, expires,
                  "ckup_", &token);
  if (!issued.ok()) return issued;
  const auto hash = Digest(token);
  auto resource = Attachment(id, name, mime);
  const auto url =
      std::string(base) + "/v1/chatkit/attachments/" + grant.session + "/" + id;
  Set(&resource, "upload_descriptor",
      Object({{"url", String(url)},
              {"method", String("PUT")},
              {"headers", Object({{"Authorization", String("Bearer " + token)},
                                  {"Content-Type", String(mime)}})}}));
  CryptoWipe(&token);
  auto reservation = Object({{"name", String(name)},
                             {"mime_type", String(mime)},
                             {"size", *size},
                             {"expires_at", Number(expires)},
                             {"token_sha256", String(hash)}});
  if (AttachmentModelInput(resource).GetString("type") == "input_image") {
    issued = IssueTicket(access, issuer, ClientTicketAudience::kChatKitPreview,
                         session_expires, "ckpv_", &token);
    if (!issued.ok()) return issued;
    Set(&reservation, "preview_sha256", String(Digest(token)));
    Set(&resource, "type", String("image"));
    Set(&resource, "preview_url", String(url + "/preview?ticket=" + token));
    CryptoWipe(&token);
  }
  for (int attempt = 0; attempt < 4; ++attempt) {
    ObjectRequest session;
    auto found = LiveSession(objects, access, grant, &session);
    if (!found.ok()) return found;
    auto& ids = Field(session.data, "attachments").array;
    if (ids.size() >=
        static_cast<uint64_t>(UploadSettings(grant).GetInt("max_files")))
      return Error(409, "chatkit_attachment_limit",
                   "Session file count reached");
    ids.push_back(String(id));
    Set(&Field(session.data, "pending_attachments"), id, reservation);
    session.mutate_before = expires;
    auto saved = objects.Execute(session, access.deadline, access.cancelled);
    if (saved.ok()) {
      saved.value = std::move(resource);
      return saved;
    }
    if (saved.commit_unknown || saved.code != "object_revision_conflict")
      return saved;
  }
  return Error(409, "chatkit_session_busy",
               "Session changed during attachment reservation");
}

ObjectReply ReadReservation(ObjectStore& objects, const StateAccess& access,
                            Grant* grant, std::string_view id,
                            std::string_view token, ReservedFile* file) {
  ObjectRequest session;
  auto found = LiveSession(objects, access, *grant, &session);
  if (!found.ok()) return found;
  auto checked = CheckReservation(session, id, token, file);
  if (!checked.ok()) return checked;
  grant->data = std::move(session.data);
  uint64_t limit = 0;
  return AttachmentLimit(*grant, &limit);
}

ObjectReply PublishAttachment(ObjectStore& objects, const BinaryAccess& access,
                              const Grant& grant, BinaryStage* stage,
                              std::string_view name, std::string_view mime,
                              std::string_view reserved_id,
                              std::string_view token) {
  uint64_t limit = 0;
  auto enabled = AttachmentLimit(grant, &limit);
  if (!enabled.ok()) return enabled;
  if (!stage || name.empty())
    return Error(400, "invalid_request", "Upload a file in this session");
  if (stage->manifest.bytes > limit)
    return Error(413, "chatkit_attachment_too_large",
                 "Attachment exceeds the session file size");
  const StateAccess state{access.scope, access.deadline, access.cancelled};
  const auto id = reserved_id.empty() ? NewObjectId(ObjectKind::kFile)
                                      : std::string(reserved_id);
  if (id.empty())
    return Error(503, "object_entropy_unavailable", "No attachment ID");
  const auto type =
      mime.empty() ? "application/octet-stream" : std::string(mime);
  auto resource = Attachment(id, name, type);
  BinaryStore binary(objects);
  bool preview_checked = false;
  for (int attempt = 0; attempt < 4; ++attempt) {
    ObjectRequest session;
    auto live = LiveSession(objects, state, grant, &session);
    if (!live.ok()) return live;
    auto& ids = Field(session.data, "attachments").array;
    const auto file_expiry = session.mutate_before;
    std::string preview_digest;
    if (reserved_id.empty()) {
      if (ids.size() >=
          static_cast<uint64_t>(UploadSettings(grant).GetInt("max_files")))
        return Error(409, "chatkit_attachment_limit",
                     "Session file count reached");
      ids.push_back(String(id));
    } else {
      ReservedFile reserved;
      auto checked = CheckReservation(session, id, token, &reserved);
      if (!checked.ok()) return checked;
      if (reserved.bytes != stage->manifest.bytes || reserved.mime != type ||
          reserved.name != name)
        return Error(400, "chatkit_attachment_mismatch",
                     "Upload differs from its reservation");
      auto& pending = Field(session.data, "pending_attachments");
      preview_digest = pending.Find(id)->GetString("preview_sha256");
      session.mutate_before = std::min(session.mutate_before,
                                       pending.Find(id)->GetInt("expires_at"));
      std::erase_if(pending.object,
                    [&](const auto& item) { return item.first == id; });
    }
    if (!preview_checked &&
        stage->manifest.bytes <= FileImageLimits{}.max_bytes &&
        AttachmentModelInput(resource).GetString("type") == "input_image") {
      std::string source, png, encoded;
      auto read = binary.ReadStage(access, *stage, FileImageLimits{}.max_bytes,
                                   &source);
      if (!read.ok()) return read;
      auto preview = MakeRasterPreview(source, access, &png);
      if (preview.ok()) {
        if (!io::EncodeBase64(png, 64U << 10, &encoded))
          return Error(503, "chatkit_preview_invalid", "Invalid preview size");
        Set(&resource, "type", String("image"));
        Set(&resource, "preview_url",
            String("data:image/png;base64," + encoded));
      } else if (preview.http_status != 503 && preview.http_status != 413) {
        return preview;
      }
      // Storage uses the session upload limit. Preview capacity and codec
      // availability do not remove a valid source from the Files interface.
      preview_checked = true;
    }
    auto file = File(state, id);
    file.operation = ObjectOperation::kPut;
    file.created_at = std::time(nullptr);
    file.expires_at = file_expiry;
    file.mutate_before = session.mutate_before;
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
    if (!preview_digest.empty())
      Set(&file.data, "chatkit_preview_sha256", String(preview_digest));
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

namespace {
ObjectReply AuthorizedPreviewFile(ObjectStore& objects,
                                  const StateAccess& access,
                                  std::string_view session_id,
                                  std::string_view file_id,
                                  std::string_view token) {
  if (!token.starts_with("ckpv_")) return Missing();
  auto file = objects.Execute(File(access, std::string(file_id)),
                              access.deadline, access.cancelled);
  if (!file.ok()) return file;
  const auto& data = Field(file.value, "data");
  if (data.GetString("chatkit_session") != session_id ||
      data.GetString("chatkit_preview_sha256") != Digest(token))
    return Missing();
  Grant grant;
  grant.session = session_id;
  ObjectRequest session;
  auto live = LiveSession(objects, access, grant, &session);
  if (!live.ok()) return live;
  if (std::ranges::none_of(Field(session.data, "attachments").array,
                           [&](const Json& value) {
                             return value.IsString() && value.str == file_id;
                           }))
    return Missing();
  return file;
}
}  // namespace

ObjectReply ReadAttachmentPreview(ObjectStore& objects,
                                  const StateAccess& access,
                                  std::string_view session_id,
                                  std::string_view file_id,
                                  std::string_view token, std::string* png) {
  png->clear();
  const auto authorized = [&] {
    return AuthorizedPreviewFile(objects, access, session_id, file_id, token);
  };
  auto file = authorized();
  if (!file.ok()) return file;
  const auto* attachment = Field(file.value, "data").Find("chatkit_attachment");
  if (!attachment || attachment->GetString("id") != file_id)
    return Error(503, "chatkit_record_invalid", "Invalid stored attachment");
  if (attachment->GetString("type") == "file") {
    // An unavailable thumbnail worker may have left a valid original at PUT.
    // Retry only this bounded read. Never mutate the file through a read
    // ticket.
    FileInputSource source;
    source.kind = FileInputSource::Kind::kStored;
    source.value = file_id;
    FileInputBudget budget;
    budget.max_bytes = FileImageLimits{}.max_bytes;
    FileInputContent content;
    const BinaryAccess binary{access.scope, access.deadline, access.cancelled};
    auto read = ReadLocalFileInput(&objects, binary, source, &budget, &content);
    if (!read.ok()) return read;
    std::string rendered;
    auto converted = MakeRasterPreview(content.bytes, binary, &rendered);
    if (!converted.ok()) return converted;
    auto live = authorized();
    if (!live.ok()) return live;
    *png = std::move(rendered);
    return {};
  }
  if (attachment->GetString("type") != "image")
    return Error(503, "chatkit_record_invalid",
                 "Invalid stored attachment type");
  const auto* url = attachment->Find("preview_url");
  constexpr std::string_view prefix = "data:image/png;base64,";
  if (!url || !url->IsString() || !url->str.starts_with(prefix) ||
      !io::DecodeBase64(std::string_view(url->str).substr(prefix.size()),
                        48U << 10, png) ||
      !png->starts_with("\x89PNG\r\n\x1a\n")) {
    png->clear();
    return Error(503, "chatkit_record_invalid",
                 "Invalid stored attachment preview");
  }
  return {};
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
    std::erase_if(Field(session.data, "pending_attachments").object,
                  [&](const auto& item) { return item.first == id; });
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

io::Json AttachmentModelInput(const Json& resource) {
  const auto media_type = resource.GetString("mime_type");
  const auto mime = http::Lower(
      http::Trim(std::string_view(media_type).substr(0, media_type.find(';'))));
  bool image = resource.GetString("type") == "image" || mime == "image/png" ||
               mime == "image/jpeg" || mime == "image/gif" ||
               mime == "image/webp";
  if (!image && (mime.empty() || mime == "application/octet-stream")) {
    const auto name = http::Lower(resource.GetString("name"));
    image = name.ends_with(".png") || name.ends_with(".jpg") ||
            name.ends_with(".jpeg") || name.ends_with(".gif") ||
            name.ends_with(".webp");
  }
  auto part = Object({{"type", String(image ? "input_image" : "input_file")},
                      {"file_id", String(resource.GetString("id"))}});
  if (image) Set(&part, "detail", String("auto"));
  return part;
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
    if (!metadata.Has("preview_url")) Set(&metadata, "preview_url", {});
    published->array.push_back(std::move(metadata));
    model->array.push_back(AttachmentModelInput(*attachment));
  }
  return {};
}
}  // namespace q4t::server::chatkit_detail
