// Image Files, inline data and authorized URLs retain identical source bytes.
#pragma once

#include "q4t/server/file_source.h"
#include "q4t/server/input_snapshot.h"
#include "q4t/vision/media_plan.h"

namespace q4t::server {
struct FileImageLimits {
  uint64_t max_bytes = 16ULL << 20;
  uint64_t max_pixels = 16ULL << 20;
  uint32_t max_edge = 16384;
};
struct FileImageInput {
  FileInputContent source;
  std::vector<std::string> urls;
  std::string detail = "auto";
  vision::MediaHeader header;
};
// Header admission is not pixel decoding. The actual media execution still
// decodes pixels and checks dimensions before any device work.
ObjectReply ReadFileImage(ObjectStore* objects, const BinaryAccess& access,
                          const FileInputSource& source,
                          const FileFetchOptions& fetch,
                          const FileImageLimits& limits,
                          FileInputBudget* budget, FileImageInput* image,
                          GrammarControl control = {});
ObjectReply SaveFileImageSnapshot(
    InputSnapshotStore& store, const BinaryAccess& access,
    std::string_view binding_sha256, const FileImageInput& image,
    const FileImageLimits& limits, const InputSnapshotLimits& storage,
    int hold_seconds, InputSnapshotRef* reference, InputSnapshotHold* hold);
ObjectReply ReadFileImageSnapshot(InputSnapshotStore& store,
                                  const BinaryAccess& access,
                                  const InputSnapshotRef& reference,
                                  std::string_view binding_sha256,
                                  const FileImageLimits& limits,
                                  const InputSnapshotLimits& storage,
                                  FileImageInput* image);
}  // namespace q4t::server
