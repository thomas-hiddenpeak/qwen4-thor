// Standard file-input source contracts and scoped, bounded byte acquisition.
#pragma once

#include <string>
#include <string_view>

#include "q4t/server/binary_store.h"

namespace q4t::server {
inline constexpr uint64_t kFileInputBytes = 50000000;

enum class FileInputProfile { kResponses, kResponsesStored, kChat };
struct FileInputSource {
  enum class Kind { kStored, kInline, kUrl };
  Kind kind = Kind::kInline;
  // Borrows the caller's unchanged JSON. Never retained beyond that request.
  std::string_view value;
  std::string filename, mime_type, detail = "auto", param;
  bool pdf_only = false;
  bool cache_breakpoint = false;
  int cache_ttl = 0;
};

struct FileInputBudget {
  uint64_t max_bytes = kFileInputBytes;
  uint64_t used_bytes = 0;
  size_t max_files = 32;
  size_t used_files = 0;
};
struct FileInputContent {
  std::string bytes, filename, mime_type, file_id, sha256;
};

// Pure validation: no file/network IO, filename is metadata, not a host path.
// One source; nullable differences match the pinned SDK content variants.
ObjectReply ParseFileInputSource(const io::Json& part, FileInputProfile profile,
                                 std::string_view param,
                                 FileInputSource* source);
ObjectReply ParseImageInputSource(const io::Json& part,
                                  FileInputProfile profile,
                                  std::string_view param,
                                  FileInputSource* source);
// Read only an inline payload or an authenticated stored File. URL acquisition
// has its own isolated network executor; this local reader never opens paths.
// Failed reads clear output and do not refund the request's attempted budget.
ObjectReply ReadLocalFileInput(ObjectStore* objects, const BinaryAccess& access,
                               const FileInputSource& source,
                               FileInputBudget* budget,
                               FileInputContent* content);
}  // namespace q4t::server
