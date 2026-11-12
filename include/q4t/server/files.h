#pragma once

#include <memory>
#include <unordered_set>

#include "q4t/server/binary_store.h"
#include "q4t/server/file_limits.h"
#include "q4t/server/multipart.h"

namespace q4t::server {
inline constexpr size_t kFileWorkingBytes = 8ULL << 20;
inline constexpr size_t kFileListWorkingBytes = 32ULL << 20;

// Files' form fields may arrive in any order. All content stays private until
// the HTTP reader and MIME parser both finish successfully. No destructor IO.
class FileUpload {
 public:
  FileUpload(ObjectStore& objects, BinaryAccess access,
             std::string_view content_type);
  const ObjectReply& Result() const { return result_; }
  ObjectReply Feed(std::string_view bytes,
                   std::chrono::steady_clock::time_point deadline);
  ObjectReply Finish();
  ObjectReply Abort(const BinaryAccess& cleanup);
  const std::string& StageId() const { return stage_.id; }

 private:
  Status BeginPart(const MultipartPart& part);
  Status Data(std::string_view bytes);
  Status EndPart();
  Status Flush();
  Status Failure(ObjectReply reply);
  BinaryStore binary_;
  BinaryAccess access_;
  BinaryStage stage_;
  std::unique_ptr<MultipartDecoder> parser_;
  std::unordered_set<std::string> seen_;
  std::string field_, text_, filename_, mime_type_, purpose_, anchor_, seconds_;
  std::string buffer_;
  ObjectReply result_;
  bool file_ended_ = false, finished_ = false;
};

class Files {
 public:
  explicit Files(ObjectStore& objects) : objects_(objects) {}
  ObjectReply Retrieve(const BinaryAccess& access, const std::string& id);
  ObjectReply Erase(const BinaryAccess& access, const std::string& id);
  // Query uses the shared decoded URL object; 1..10000, default10000/desc.
  ObjectReply List(const BinaryAccess& access, const io::Json& query);
  ObjectReply Open(const BinaryAccess& access, const std::string& id,
                   BinaryRead* read, std::string* mime_type = nullptr,
                   uint64_t max_bytes = kBinaryMaxBytes);

 private:
  ObjectReply Stored(const BinaryAccess& access, const std::string& id);
  ObjectStore& objects_;
};
}  // namespace q4t::server
