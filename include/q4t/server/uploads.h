#pragma once

#include <memory>

#include "q4t/server/binary_store.h"
#include "q4t/server/multipart.h"

namespace q4t::server {
inline constexpr uint64_t kUploadPartBytes = 64ULL << 20;
inline constexpr uint64_t kUploadPartWireBytes = kUploadPartBytes + 65536;
inline constexpr size_t kUploadMaxParts = 8192;
inline constexpr size_t kUploadCompleteWorkingBytes = 32ULL << 20;

class Uploads {
 public:
  explicit Uploads(ObjectStore& objects) : objects_(objects) {}
  ObjectReply Create(const BinaryAccess& access, const io::Json& body);
  ObjectReply Cancel(const BinaryAccess& access, const std::string& id);
  ObjectReply Complete(const BinaryAccess& access, const std::string& id,
                       const io::Json& body);
  // Internal reconciliation of the original ID after an unknown receipt.
  // The standard API has no retrieve route; this does not add a public one.
  ObjectReply Retrieve(const BinaryAccess& access, const std::string& id);

 private:
  ObjectStore& objects_;
};

// One multipart Part. Constructor/Feed never publish a visible part; only a
// successful Finish after complete HTTP framing can atomically link it to the
// still-pending Upload. Known CAS conflicts retry the same stage/part identity.
class UploadPart {
 public:
  UploadPart(ObjectStore& objects, BinaryAccess access, std::string upload_id,
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
  Status Flush();
  Status Failure(ObjectReply reply);
  ObjectStore& objects_;
  BinaryStore binary_;
  BinaryAccess access_;
  std::string upload_id_, buffer_;
  int64_t expires_ = 0;
  BinaryStage stage_;
  std::unique_ptr<MultipartDecoder> parser_;
  ObjectReply result_;
  bool seen_ = false, ended_ = false, finished_ = false;
};
}  // namespace q4t::server
