#pragma once

#include "q4t/server/files.h"
#include "q4t/server/http_request.h"
#include "q4t/server/uploads.h"

namespace q4t::server {
// One authenticated Files/Uploads operation. This implementation is used by the
// actual engine and model-free CPU transport fixtures; no secondary web app.
class FileHttpRequest {
 public:
  FileHttpRequest(ObjectStore& objects, BinaryAccess access)
      : objects_(objects), access_(std::move(access)) {}
  ~FileHttpRequest();
  ObjectReply Prepare(std::string_view method, std::string_view target,
                      std::string_view content_type, HttpBodyStream* stream);
  uint64_t WireLimit() const { return wire_limit_; }
  const ObjectReply* UploadFailure() const {
    if (upload_ && !upload_->Result().ok()) return &upload_->Result();
    return part_ && !part_->Result().ok() ? &part_->Result() : nullptr;
  }
  void Serve(int fd);

 private:
  void Content(int fd);
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
