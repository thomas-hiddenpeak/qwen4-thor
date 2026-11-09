#pragma once

#include "q4t/server/files.h"
#include "q4t/server/http_request.h"

namespace q4t::server {
// One authenticated HTTP operation. Same implementation is exercised by the
// actual engine and model-free CPU transport fixtures; no secondary web app.
class FileHttpRequest {
 public:
  FileHttpRequest(ObjectStore& objects, BinaryAccess access)
      : objects_(objects), access_(std::move(access)) {}
  ~FileHttpRequest();
  ObjectReply Prepare(std::string_view method, std::string_view target,
                      std::string_view content_type, HttpBodyStream* stream);
  uint64_t WireLimit() const { return upload_ ? kFileWireBytes : 0; }
  const ObjectReply* UploadFailure() const {
    return upload_ && !upload_->Result().ok() ? &upload_->Result() : nullptr;
  }
  void Serve(int fd);

 private:
  void Content(int fd);
  ObjectStore& objects_;
  BinaryAccess access_;
  std::string method_, id_;
  io::Json query_;
  bool collection_ = false, content_ = false;
  std::unique_ptr<FileUpload> upload_;
};
}  // namespace q4t::server
