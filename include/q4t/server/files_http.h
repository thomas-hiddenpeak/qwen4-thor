#pragma once

#include <memory>

#include "q4t/server/files.h"
#include "q4t/server/http_request.h"
#include "q4t/server/uploads.h"
#include "q4t/server/skill_upload.h"

namespace q4t::server {
// Streams and releases an already pinned immutable manifest, including on a
// disconnected reader. Its caller supplies the authenticated resource check.
void SendPinnedBinary(int fd, BinaryStore& binary, const BinaryAccess& access,
                      BinaryRead& read, std::string_view mime_type = "application/octet-stream");

// One authenticated Files/Uploads operation. This implementation is used by the
// actual engine and model-free CPU transport fixtures; no secondary web app.
class FileHttpRequest {
 public:
  FileHttpRequest(ObjectStore& objects, BinaryAccess access)
      : objects_(objects), access_(std::move(access)) {}
  ~FileHttpRequest();
  ObjectReply Prepare(std::string_view method, std::string_view target,
                      std::string_view content_type, HttpBodyStream* stream, std::string_view accept = {});
  ObjectReply PrepareChatKit(std::string_view token,
                             std::string_view content_type,
                             HttpBodyStream* stream);
  uint64_t WireLimit() const { return wire_limit_; }
  const ObjectReply* UploadFailure() const {
    if (skill_upload_ && !skill_upload_->Result().ok()) return &skill_upload_->Result();
    if (upload_ && !upload_->Result().ok()) return &upload_->Result();
    return part_ && !part_->Result().ok() ? &part_->Result() : nullptr;
  }
  void Serve(int fd);

 private:
  void Content(int fd);
  ObjectReply PrepareSkill(std::string_view content_type, HttpBodyStream* stream);
  void ServeSkill(int fd);
  std::string skill_target_;
  bool skill_json_content_ = false;
  std::unique_ptr<SkillUpload> skill_upload_;
  ObjectReply PrepareContainer(std::string_view content_type,
                               HttpBodyStream* stream);
  void PrepareFileStream(HttpBodyStream* stream);
  std::string container_target_;
  ObjectReply PrepareUpload(std::string_view method, std::string_view target,
                            std::string_view content_type,
                            HttpBodyStream* stream);
  void ServeUpload(int fd);
  ObjectStore& objects_;
  BinaryAccess access_;
  std::string method_, id_;
  io::Json query_;
  bool collection_ = false, content_ = false;
  std::unique_ptr<FileUpload> upload_;
  std::unique_ptr<UploadPart> part_;
  std::string upload_action_, json_body_;
  uint64_t wire_limit_ = 0;
};
}  // namespace q4t::server
